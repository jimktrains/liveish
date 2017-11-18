#!/usr/bin/env python3

import sys
import array
import re
import pty
import os
import socket
import queue
import select
from threading import Thread, Barrier
from subprocess import Popen, PIPE
import shlex
import fcntl
import signal
import selectors
import termios
import time

config = {
    'command': '/usr/bin/env bash',
    'serverhost': '127.0.0.1',
    'serverport': 7890,
    'viewport': '80x24',
    'env.TERM': 'vt100',
    'auto-delay': '0.5',
}


def set_nonblocking_open(fd, mode):
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    return os.fdopen(fd, mode)

def get_env(options):
    env = {}
    for k in options:
        if k[0:4] == 'env.':
            (_, ek) = k.split('.', 1)
            env[ek] = options[k]
    return env

def ptyfork(argv, env=None, size=None):
    child_pid, child_fd = pty.fork()
    if child_pid == 0:
        environ = os.environ.copy()
        if env is not None:
            environ.update(env)
        os.execve(argv[0], argv, environ)
    else:
        if size is not None:
            sizebuf = array.array('h', list(reversed(size)))
            fcntl.ioctl(child_fd, termios.TIOCSWINSZ, sizebuf)
        return (child_pid, child_fd)

def create_send_to_all(host, port):
    broadcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def tmp(x):
        broadcast.sendto(x.encode('utf8'), (host, int(port)))
    return tmp

def event_loop(readers, env):
    sel = selectors.DefaultSelector()
    for (channel, callback) in readers:
        sel.register(channel, selectors.EVENT_READ, callback)

    keep_selecting = True
    while keep_selecting:
        events = sel.select()
        for key, mask in events:
            callback = key.data
            r = callback(key.fileobj, mask, env)
            if r is not None:
                if type(r) == tuple:
                    (k, v) = r
                    env[k] = v
                else:
                    keep_selecting = r

    for (channel, callback) in readers:
        sel.unregister(channel)

    return env

# ^` to get a ^ (^` would render a space, usful since we strip lines
# ^a to get a ^
def ctrl_replacer(match):
    c = match.group(1)
    if (c == 'a'):
        return '^'
    return chr(ord(c) - ord('@'))
        
def format_command(cmd):
    cmd = re.sub('\^(.)', ctrl_replacer, cmd)
    return cmd

def process_thread(output_callback, noecho_callback, options, term_in, cmdq, meta_out, application_launched_barrier):
    command = shlex.split(options['command'])
    size = list(map(int, options['viewport'].split('x', 1)))

    (child_pid, child_fd) = ptyfork(command, get_env(options), size)

    cstdout = set_nonblocking_open(child_fd, 'r')
    cstdin = set_nonblocking_open(child_fd, 'w')

    cmdq = set_nonblocking_open(cmdq.fileno(), 'r')
    term_in = set_nonblocking_open(term_in.fileno(), 'r')

    s = ("child-pid " + str(child_pid)).encode('utf8')
    meta_out.sendall(chr(len(s)).encode('utf8'))
    meta_out.sendall(s)
    meta_out.sendall(chr(4).encode('utf8'))
    meta_out.sendall(b"done")
    application_launched_barrier.wait()

    def handle_termin(conn, mask, env):
        line = conn.readline()
        if len(line) != 1:
            line = line.strip()
        cstdin.write(line)
        cstdin.flush()
    def handle_cstdout(conn, mask, env):
        v = conn.read()
        if env['echo']:
            output_callback(v)
        else:
            noecho_callback(v)
    def handle_cmdq(cmdq, mask, env):
        cmd = cmdq.readline()
        while cmd:
            cmd = cmd.strip()
            if cmd == 'exit':
                return False
            else:
                (op, param) = cmd.split(' ', 1)
                if op in ["echo", "no-echo"] :
                    param = format_command(param)
                    cstdin.write(param)
                    cstdin.flush()
                    return ('echo', op == "echo")
            cmd = cmdq.readline()


    readers = [
        [term_in, handle_termin],
        [cstdout, handle_cstdout],
        [cmdq, handle_cmdq],
    ]

    event_loop(readers, {"echo": True})

    os.kill(child_pid, signal.SIGTERM)

with open(sys.argv[1]) as scriptfile:
    told = 0
    line = scriptfile.readline()
    while len(line) > 0 and line[0] == '!':
        (key, value) = line.split(" ", 1)
        key = key[1:]
        config[key] = value
        told = scriptfile.tell()
        line = scriptfile.readline()
    # Rewind a little so that we can start at the script
    scriptfile.seek(told)

    application_launched_barrier = Barrier(2, timeout=5)

    termin_in, termin_out = socket.socketpair()
    cmdq_in, cmdq_out = socket.socketpair()
    meta_in, meta_out = socket.socketpair()

    sendtoall = create_send_to_all(config['serverhost'], config['serverport'])

    thread = Thread(target = process_thread, args = (
        sendtoall, 
        lambda x : sys.stdout.write(x),
        config, 
        termin_out, 
        cmdq_out, 
        meta_out,
        application_launched_barrier
    ))
    thread.start()

    application_launched_barrier.wait()

    meta = None
    def handle_meta(conn, mask, env):
        length = ord(conn.recv(1))
        line = conn.recv(length).decode('utf8')
        if line == 'done':
            return False
        (key, val) = line.split(' ')
        return (key, val)

    meta = event_loop([[meta_in, handle_meta]], {})
    env = get_env(config)

    print(config['command'] + " started on pid " + str(meta['child-pid']))
    print("Data being sent to UDP " + config['serverhost'] + ":"  + str(config['serverport']))
    print("Environment: ")
    for k in env:
        print("\t" + k + "\t" + env[k])
    print("================")



    try:
        for line in scriptfile:
            if line[0] == '#':
                continue
            elif line[0] == '!':
                (key, value) = line.split(" ", 1)
                key = key[1:]
                config[key] = value
            elif line[0] == '>':
                cmd = line[2:]
                if (line[1] == ' '):
                    input("type? " + cmd.strip())
                elif (line[1] == '\\'):
                    input("type&run? " + cmd.strip())
                elif (line[1] == '+'):
                    print("auto-run? " + cmd.strip())
                cmdq_in.sendall(("echo " + cmd).encode('utf8'))
                if (line[1] == ' '):
                    input("run?")
                    cmdq_in.sendall(b"echo ^J\n")
                if (line[1] == '+'):
                    cmdq_in.sendall(b"echo ^J\n")
                    time.sleep(float(config['auto-delay']))
            elif line[0] == '+':
                cmd = line[2:].strip()
                print("auto command " + cmd)
                cmdq_in.sendall(("no-echo " + cmd + "^J\n").encode('utf8'))
                time.sleep(float(config['auto-delay']))
            elif line[0] == '@':
                duration = float(line[2:])
                print("sleeping for " + str(duration))
                time.sleep(duration)
            else:
                print(line.strip())
        input("exit?")
    except KeyboardInterrupt:
        pass
    except :
        print(sys.exc_info()[0])
    cmdq_in.sendall(b"exit\n")
