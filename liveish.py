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
            sizebuf = array.array('h', list(reversed(size)))
            fcntl.ioctl(self.child_fd, termios.TIOCSWINSZ, sizebuf)

    def read(self, n = 1024):
        try:
            return os.read(self.child_fd, n)
        except BlockingIOError:
            pass

    def write(self, b):
        return os.write(self.child_fd, b)

    def set_nonblocking(self, fd):
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


    def fileno(self):
        return self.child_fd

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        os.kill(self.child_pid, signal.SIGTERM)


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

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
    randnum = random.gauss(µ, σ)
    while randnum < minn or randnum < (µ + (maxx*σ)):
        randnum = random.gauss(µ, σ)
    return randnum

class MQPID:
    def __init__(self, pid):
        self.pid = pid

def worker(command, cmdq, metaq, exit_now, echo_callback, noecho_callback, application_launched_barrier):
    with PTYFork(command) as terminal:
        metaq.put(MQPID(terminal.child_pid))
        application_launched_barrier.wait()

        echo = True
        sleep_until = False
        last_input = False
        last_output_at = time.time()
        wait_for_output = False
        exit_after_output = False
        while True and not exit_now.is_set():
            bytes_from_terminal = terminal.read(128)
            if bytes_from_terminal is not None:
                last_output_at = time.time()
                if echo:
                    echo_callback(bytes_from_terminal)
                else:
                    noecho_callback(bytes_from_terminal)

            if wait_for_output != False:
                if (time.time() - last_output_at) > wait_for_output:
                    wait_for_output = False
                    if exit_after_output:
                        break
            elif last_input != False:
                if last_input.next_time < time.time():
                    if last_input.delay_mean == 0:
                        terminal.write(last_input.str_in.encode('utf8'))
                        last_input = False
                    else:
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
                sleep_until = False
                if not cmdq.empty():
                    cmd = cmdq.get()
                    if isinstance(cmd, QWaitForOutput):
                        wait_for_output = cmd.wait_for_output
                        exit_after_output = isinstance(cmd, QExit)
                    elif isinstance(cmd, QEcho):
                        echo = cmd.echo
                    elif isinstance(cmd, QSleep):
                        sleep_until = cmd.until
                    elif isinstance(cmd, QInput):
                        last_input = cmd


config = {
    'command': '/usr/bin/env bash',
    'host': '127.0.0.1',
    'port': '7890',
    'auto-delay': 0.500,
}

q = queue.Queue()
m = queue.Queue()
hostport = (config['host'], int(config['port']))
print("Sending output to " + ":".join(map(str, hostport)))

with open(sys.argv[1]) as scriptfile:
    told = 0
    line = scriptfile.readline().strip()
    while len(line) > 0 and line[0] == '!':
        (key, value) = line.split(" ", 1)
        key = key[1:]
        config[key] = value
        told = scriptfile.tell()
        line = scriptfile.readline()
    # Rewind a little so that we can start at the script
    scriptfile.seek(told)


    application_launched_barrier = Barrier(2, timeout=5)
    exit_now = Event()
    thread = Thread(target = worker, args = (
        '/usr/bin/env bash',
        q,
        m,
        exit_now,
        lambda x : sock.sendto(x, hostport),
        lambda x : (sys.stdout.buffer.write(x), sys.stdout.buffer.flush()),
        application_launched_barrier
    ))
    thread.start()

    application_launched_barrier.wait()

    while not m.empty():
        metadata = m.get()
        if isinstance(metadata, MQPID):
            print(config['command'] + " started at pid " + str(metadata.pid))

    try:
        for line in scriptfile:
            line = line.strip()
            if len(line) == 0:
                print()
            elif line[0] == '#':
                continue
            elif line[0] == '!':
                (key, value) = line.split(" ", 1)
                key = key[1:]
                config[key] = value
            elif line[0] == '>':
                cmd = line[2:]
                if (line[1] == ' '):
                    input("type? " + cmd)
                elif (line[1] == '\\'):
                    input("type&run? " + cmd)
                elif (line[1] == '+'):
                    print("auto-run? " + cmd)
                q.put(QInput(cmd))
                if (line[1] == ' '):
                    input("run?")
                    q.put(QEnter())
                if (line[1] == '+'):
                    q.put(QEnter())
                    time.sleep(float(config['auto-delay']))
            elif line[0] == '+':
                cmd = line[2:]
                print("auto command " + cmd)
                q.put(QEcho(False))
                q.put(QInput(cmd))
                q.put(QEnter())
                q.put(QWaitForOutput(config['auto-delay']))
                q.put(QEcho(True))
                time.sleep(config['auto-delay'])
            elif line[0] == '@':
                duration = float(line[2:])
                print("sleeping for " + str(duration))
                time.sleep(duration)
            else:
                print(line.strip())
        input("exit?")
    except KeyboardInterrupt:
        pass
    except:
        print(sys.exc_info()[0])
    finally:
        q.put(QExitNow())
        exit_now.set()
