#!/usr/env/env python3

import sys
import array
import re
import pty
import os
import socket
import queue
import select
from threading import Thread, Barrier, Event
from subprocess import Popen, PIPE
import shlex
import fcntl
import signal
import selectors
import termios
import time
import random 

class PTYFork:
    def __init__(self, argv, env=None, size=None):
        if isinstance(argv, str):
            argv = shlex.split(argv)
        (self.child_pid, self.child_fd) = os.forkpty()

        if self.child_pid == 0:
            environ = os.environ.copy()
            if env is not None:
                environ.update(env)
            os.execve(argv[0], argv, environ)
            sys.exit(0)

        os.set_blocking(self.child_fd, False) 

        if size is not None:
            if 'x' in size:
                size = list(map(int, size.split('x',1)))
            sizebuf = array.array('h', list(reversed(size)))
            fcntl.ioctl(self.child_fd, termios.TIOCSWINSZ, sizebuf)

    def read(self, n = 1024):
        try:
            return os.read(self.child_fd, n)
        except BlockingIOError:
            pass

    def write(self, b):
        return os.write(self.child_fd, b)

    def fileno(self):
        return self.child_fd

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        os.kill(self.child_pid, signal.SIGTERM)


class QWaitForOutput:
    def __init__(self, wait_for_output = 2):
        self.wait_for_output = wait_for_output

class QExit(QWaitForOutput):
    def __init__(self, wait_for_output = 2):
        super().__init__(wait_for_output)

class QExitNow(QExit):
    def __init__(self):
        super().__init__(0)

class QEcho:
    def __init__(self, echo = True):
        self.echo = echo

class QSleep:
    def __init__(self, secs):
        self.until = time.time() + secs

class QExpect:
    def __init__(self, text):
        self.text = text

class QInput:
    def __init__(self, str_in, delay_mean=0, delay_std=0):
        self.str_in = self.format(str_in)
        self.delay_mean = delay_mean
        self.delay_std = delay_std
        self.next_time = 0

    def format(self, cmd):
        # ^` to get a ^ (^` would render a space, usful since we strip lines
        # ^a to get a ^
        def ctrl_replacer(match):
            c = match.group(1)
            if (c == 'a'):
                return '^'
            return chr(ord(c) - ord('@'))
                
        cmd = re.sub('\^(.)', ctrl_replacer, cmd)
        return cmd

class QEnter(QInput):
    def __init__(self):
        super().__init__("^J", 0, 0)

def gauss_bound(µ, σ, minn, maxx):
    µ = float(µ)
    σ = float(σ)
    randnum = random.gauss(µ, σ)
    while randnum < minn or randnum < (µ + (maxx*σ)):
        randnum = random.gauss(µ, σ)
    return randnum

class MQPID:
    def __init__(self, pid):
        self.pid = pid

def worker(command, cmdq, metaq, exit_now, not_accepting_input, echo_callback, noecho_callback, viewport, env, application_launched_barrier):
    try:
        with PTYFork(command, env, viewport) as terminal:
            metaq.put(MQPID(terminal.child_pid))
            application_launched_barrier.wait()

            echo = True
            sleep_until = False
            last_input = False
            last_output_at = time.time()
            wait_for_output = False
            exit_after_output = False
            expect_text = False
            text_thus_far = None
            while True and not exit_now.is_set():
                bytes_from_terminal = terminal.read(128)
                if bytes_from_terminal is not None:
                    last_output_at = time.time()
                    if echo:
                        echo_callback(bytes_from_terminal)
                    else:
                        noecho_callback(bytes_from_terminal)

                time_since_last_output = time.time() - last_output_at

                if wait_for_output != False:
                    not_accepting_input.set()
                    if time_since_last_output > wait_for_output:
                        wait_for_output = False
                        if exit_after_output:
                            break
                elif expect_text != False:
                    not_accepting_input.set()
                    if bytes_from_terminal is not None:
                        if text_thus_far is None:
                            text_thus_far = bytes_from_terminal.decode('utf8')
                        else:
                            text_thus_far += bytes_from_terminal.decode('utf8')
                        if expect_text in text_thus_far:
                            text_thus_far = None
                            expect_text = False
                elif last_input != False:
                    if last_input.next_time < time.time():
                        if last_input.delay_mean == 0:
                            terminal.write(last_input.str_in.encode('utf8'))
                            last_input = False
                        else:
                            not_accepting_input.set()
                            terminal.write(last_input.str_in[0].encode('utf8'))
                            wait_time = gauss_bound(last_input.delay_mean, last_input.delay_std, 0, 3) / 1000.0
                            last_input.next_time = time.time() + wait_time
                            last_input.str_in = last_input.str_in[1:]
                            if len(last_input.str_in) == 0:
                                last_input = False
                elif sleep_until != False:
                    if time.time() > sleep_until:
                        sleep_until = False
                else:
                    not_accepting_input.clear()
                    if not cmdq.empty():
                        cmd = cmdq.get()
                        if isinstance(cmd, QWaitForOutput):
                            wait_for_output = float(cmd.wait_for_output)
                            exit_after_output = isinstance(cmd, QExit)
                        elif isinstance(cmd, QEcho):
                            echo = cmd.echo
                        elif isinstance(cmd, QSleep):
                            sleep_until = cmd.until
                        elif isinstance(cmd, QInput):
                            last_input = cmd
                        elif isinstance(cmd, QExpect):
                            expect_text = cmd.text
    finally:
        not_accepting_input.clear()
        exit_now.set()

config = {
    'command': '/usr/bin/env bash',
    'host': '127.0.0.1',
    'port': '7890',
    'scripthost': '127.0.0.1',
    'scriptport': '7891',
    'auto-delay': 0.500,
    'type.average_delay': 20,
    'type.stddev_delay': 30,
    'viewport': "80x24",
    'env': {}
}

q = queue.Queue()
m = queue.Queue()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

def normalize_line(line):
    line = line.strip()
    if len(line) == 0:
        line = "%pause-script"
    elif line[0] == '#':
        comment = ""
        if len(line) != 1:
            comment = line[1:]
        line = "%comment " + comment
    elif line[0] == '!':
        line = line[1:]
        line = "%config " + line
    elif line[0] == '>':
        if len(line) == 1:
            line = ">\^J"
        rest = line[2:]
        if line[1] == '\\':
            line = "%type " + rest
        elif line[1] == '+':
            line = "%auto-type-run " + rest
        else:
            line = "%type-run " + rest
    elif line[0] == '+':
        cmd = line[2:]
        line = "%auto-run-no-echo " + cmd
    elif line[0] == '$':
        line = "%wait-for-output"
    elif line[0] == '@':
        duration = float(line[2:])
        line = "%sleep " + str(duration)

    return line

vt100_escape_codes = {
"setnl": "^[[20h",
"setappl": "^[[?1h",
"setcol": "^[[?3h",
"setsmooth": "^[[?4h",
"setrevscrn": "^[[?5h",
"setorgrel": "^[[?6h",
"setwrap": "^[[?7h",
"setrep": "^[[?8h",
"setinter": "^[[?9h",

"setlf": "^[[20l",
"setcursor": "^[[?1l",
"setvt52": "^[[?2l",
"resetcol": "^[[?3l",
"setjump": "^[[?4l",
"setnormscrn": "^[[?5l",
"setorgabs": "^[[?6l",
"resetwrap": "^[[?7l",
"resetrep": "^[[?8l",
"resetinter": "^[[?9l",

"altkeypad": "^[=",
"numkeypad": "^[>",

"setukg0": "^[(A",
"setukg1": "^[)A",
"setusg0": "^[(B",
"setusg1": "^[)B",
"setspecg0": "^[(0",
"setspecg1": "^[)0",
"setaltg0": "^[(1",
"setaltg1": "^[)1",
"setaltspecg0": "^[(2",
"setaltspecg1": "^[)2",

"setss2": "^[N",
"setss3": "^[O",

"modesoff": "^[[m",
"modesoff": "^[[0m",
"bold": "^[[1m",
"lowint": "^[[2m",
"underline": "^[[4m",
"blink": "^[[5m",
"reverse": "^[[7m",
"invisible": "^[[8m",

"setwin": "^[[;r",

#"cursorup(n)": "^[[A",
#"cursordn(n)": "^[[B",
#"cursorrt(n)": "^[[C",
#"cursorlf(n)": "^[[D",
"cursorhome": "^[[H",
"cursorhome": "^[[;H",
#"cursorpos(v,h)": "^[[;H",
"hvhome": "^[[f",
"hvhome": "^[[;f",
#"hvpos(v,h)": "^[[;f",
"index": "^[D",
"revindex": "^[M",
"nextline": "^[E",
"savecursor": "^[7",
"restorecursor": "^[8",

"tabset": "^[H",
"tabclr": "^[[g",
"tabclr": "^[[0g",
"tabclrall": "^[[3g",

"dhtop": "^[#3",
"dhbot": "^[#4",
"swsh": "^[#5",
"dwsh": "^[#6",

"cleareol": "^[[K",
"cleareol": "^[[0K",
"clearbol": "^[[1K",
"clearline": "^[[2K",

"cleareos": "^[[J",
"cleareos": "^[[0J",
"clearbos": "^[[1J",
"clearscreen": "^[[2J",

"devstat": "^[5n",
"termok": "^[0n",
"termnok": "^[3n",

"getcursor": "^[6n",
"cursorpos": "^[;R",

"ident": "^[[c",
"ident": "^[[0c",
"gettype": "^[[?1;0c",

"reset": "^[c",

"align": "^[#8",
"testpu": "^[[2;1y",
"testlb": "^[[2;2y",
"testpurep": "^[[2;9y",
"testlbrep": "^[[2;10y",

"ledsoff": "^[[0q",
"led1": "^[[1q",
"led2": "^[[2q",
"led3": "^[[3q",
"led4": "^[[4q",


"setansi": "^[<",

"altkeypad": "^[=",
"numkeypad": "^[>",

"setgr": "^[F",
"resetgr": "^[G",

"cursorup": "^[A",
"cursordn": "^[B",
"cursorrt": "^[C",
"cursorlf": "^[D",
"cursorhome": "^[H",
#"cursorpos(v,h)": "^[",
"revindex": "^[I",

"cleareol": "^[K",
"cleareos": "^[J",

"ident": "^[Z",
"identresp": "^[/Z",
"PF1": "^[OP",
"PF2": "^[OQ",
"PF3": "^[OR",
"PF4": "^[OS",
"up": "^[A",
"down": "^[B",
"right": "^[C",
"left": "^[D",
}

with open(sys.argv[1]) as scriptfile:
    told = 0
    line = scriptfile.readline().strip()
    waiting_on_typing = False
    # .tell doesn't seem to work once an iterator is started, hence not
    # using a for-loop here
    while len(line) > 0:
        if len(line) == 0:
            continue
        elif line[0] == '!':
            (key, value) = line.split(" ", 1)
            key = key[1:]
            config[key] = value
        elif line[0:4] == '%env':
            (key, value) = line[5:].split(" ", 1)
            config['env'][key] = value
        elif line[0] == '#':
            continue
        else:
            break
        told = scriptfile.tell()
        line = scriptfile.readline().strip()
    # Rewind a little so that we can start at the script
    scriptfile.seek(told)

    application_launched_barrier = Barrier(2, timeout=5)
    exit_now = Event()
    not_accepting_input = Event()


    thread = Thread(target = worker, args = (
        config['command'],
        q,
        m,
        exit_now,
        not_accepting_input,
        lambda x : sock.sendto(x, hostport),
        lambda x : (sys.stdout.buffer.write(x), sys.stdout.buffer.flush()),
        config['viewport'],
        config['env'],
        application_launched_barrier
    ))
    thread.start()

    hostport = (config['host'], int(config['port']))
    scripthostport = (config['scripthost'], int(config['scriptport']))
    print("Sending output to " + ":".join(map(str, hostport)))
    print("Sending script to " + ":".join(map(str, scripthostport)))

    application_launched_barrier.wait()

    while not m.empty():
        metadata = m.get()
        if isinstance(metadata, MQPID):
            print(config['command'] + " started at pid " + str(metadata.pid))

    try:
        q.put(QWaitForOutput(config['auto-delay']))
        for line in scriptfile:
            if exit_now.is_set():
                break
            line = normalize_line(line)

            cmd = None
            if line[0] == '%':
                cmd = line
                if ' ' in line:
                    cmd, line = line.split(' ', 1)
                cmd = cmd[1:]

            keep_trying = True
            notified = False
            waiting_notified = False
            while keep_trying and not exit_now.is_set():
                keep_trying = False

                if cmd == 'pause-script':
                    input('<paused>')
                    print()
                    sock.sendto("\n".encode('utf8'), scripthostport)
                elif cmd == "comment":
                    pass
                elif cmd == "config":
                    (key, value) = line.split(" ", 1)
                    print(key, value)
                    config[key] = value
                elif cmd and "type" in cmd:
                    if not q.empty() or not_accepting_input.is_set():
                        keep_trying = True
                        time.sleep(0.1)
                        if not notified:
                            print("<Waiting on to accept input>")
                            notified = True
                    else:
                        if len(line) == 0:
                            line = '^J'
                        elif (len(line) > 2 and line[-2:] == "^["):
                            q.put(QInput('^@'))

                        if cmd == "type-run":
                            input("type-run! " + line)
                            q.put(QInput(line, config['type.average_delay'], config['type.stddev_delay']))
                            input("run?")
                            q.put(QEnter())
                            q.put(QWaitForOutput(config['auto-delay']))
                        elif cmd == "auto-type-run":
                            print("auto-run! " + line)
                            q.put(QInput(line, config['type.average_delay'], config['type.stddev_delay']))
                            q.put(QEnter())
                            q.put(QWaitForOutput(config['auto-delay']))
                            time.sleep(float(config['auto-delay']))
                        elif cmd == "type-auto-run":
                            input("type-auto-run? " + line)
                            q.put(QInput(line, config['type.average_delay'], config['type.stddev_delay']))
                            q.put(QEnter())
                            q.put(QWaitForOutput(config['auto-delay']))
                        elif cmd == "type":
                            input("type-only? " + line)
                            q.put(QInput(line, config['type.average_delay'], config['type.stddev_delay']))
                            q.put(QWaitForOutput(config['auto-delay']))
                elif cmd == "auto-run-no-echo":
                    if not q.empty() or not_accepting_input.is_set():
                        keep_trying = True
                        time.sleep(0.1)
                        if not notified:
                            print("<Waiting on to accept input>")
                            notified = True
                    else:
                        print("auto command " + line)
                        q.put(QEcho(False))
                        q.put(QInput(line))
                        q.put(QEnter())
                        q.put(QWaitForOutput(config['auto-delay']))
                        q.put(QEcho(True))
                elif cmd == "wait-for-output":
                    q.put(QWaitForOutput(config['auto-delay']))
                elif cmd == "expect":
                    q.put(QExpect(line))
                elif cmd == "sleep":
                    duration = float(line)
                    print("sleeping for " + str(duration))
                    time.sleep(duration)
                elif cmd == "vt100":
                    if line in vt100_escape_codes:
                        q.put(QInput(vt100_escape_codes[line]))
                elif cmd == "exit":
                    if not q.empty() or not_accepting_input.is_set():
                        keep_trying = True
                        time.sleep(0.1)
                        if not notified:
                            print("<Waiting on to accept input>")
                            notified = True
                    else:
                        exit_now.set()
                else:
                    print(line.strip())
                    sock.sendto((line + "\n").encode('utf8'), scripthostport)
                    

        notified = False
        keep_trying = True
        while keep_trying and not exit_now.is_set():
            if not q.empty() or not_accepting_input.is_set():
                time.sleep(0.1)
                if not notified:
                    print("<Waiting on to accept input>")
                    notified = True
            else:
                keep_trying = False
        input("exit?")
    except KeyboardInterrupt:
        pass
    finally:
        q.put(QExitNow())
        exit_now.set()
