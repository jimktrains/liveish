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

config = {
    'command': '/usr/bin/env bash',
    'serverhost': '127.0.0.1',
    'serverport': 7890,
    'viewport': '80x24',
    'env.TERM': 'vt100',
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

def event_loop(readers):
    sel = selectors.DefaultSelector()
    for (channel, callback) in readers:
        sel.register(channel, selectors.EVENT_READ, callback)

    keep_selecting = True
    ret = {}
    while keep_selecting:
        events = sel.select()
        for key, mask in events:
            callback = key.data
            r = callback(key.fileobj, mask)
            if r is not None:
                if type(r) == tuple:
                    (k, v) = r
                    ret[k] = v
                else:
                    keep_selecting = r

    for (channel, callback) in readers:
        sel.unregister(channel)

    return ret

def process_thread(output_callback, options, term_in, cmdq, meta_out, application_launched_barrier):
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

    def handle_termin(conn, mask):
        line = conn.readline()
        if len(line) != 1:
            line = line.strip()
        cstdin.write(line)
        cstdin.flush()
    def handle_cstdout(conn, mask):
        v = conn.read()
        output_callback(v)
    def handle_cmdq(cmdq, mask):
        cmd = cmdq.readline().strip()
        if cmd == 'exit':
            return False

    readers = [
        [term_in, handle_termin],
        [cstdout, handle_cstdout],
        [cmdq, handle_cmdq],
    ]

    event_loop(readers)

    os.kill(child_pid, signal.SIGTERM)
        


with open('script') as scriptfile:
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
        config, 
        termin_out, 
        cmdq_out, 
        meta_out,
        application_launched_barrier
    ))
    thread.start()

    application_launched_barrier.wait()

    meta = None
    def handle_meta(conn, mask):
        length = ord(conn.recv(1))
        line = conn.recv(length).decode('utf8')
        if line == 'done':
            return False
        (key, val) = line.split(' ')
        return (key, val)

    meta = event_loop([[meta_in, handle_meta]])
    env = get_env(config)

    print(config['command'] + " started on pid " + str(meta['child-pid']))
    print("Data being sent to UDP " + config['serverhost'] + ":"  + str(config['serverport']))
    print("Environment: ")
    for k in env:
        print("\t" + k + "\t" + env[k])
    print("================")


    # ^` to get a ^ (^` would render a space, so that's pointless :))
    def ctrl_replacer(match):
        c = match.group(1)
        if (c == '`'):
            return '^'
        return chr(ord(c) - ord('@'))

    try:
        for line in scriptfile:
            if line[0] == '>':
                cmd = line[2:]
                input("type? " + cmd.strip())
                cmd = re.sub('\^(.)', ctrl_replacer, cmd)
                termin_in.sendall(cmd.encode('utf8'))
                if (line[1] != '\\'):
                    input("run?")
                    termin_in.sendall(b"\n")
            else:
                print(line.strip())
        input("exit?")
    except KeyboardInterrupt:
        pass
    except :
        print(sys.exc_info()[0])
    cmdq_in.sendall(b"exit\n")
