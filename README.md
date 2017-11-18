liveish - script your speech and your commands
----------------------------------------------

Take a script file in the format of:

* line of with !<key> <value> will set the config
* line begining with > is sent to the command. (You'll be asked to type, and to hit enter)
* line begining with >\ is sent to the command. (You'll be asked to type)
* line begining with > will have ^<x> replaced with ctrl + <x>
* line begining with >+ will be automatically typed
* line begining with + will be automatically types
* line begining with @ will sleep for the number of seconds given
* line begining with # will be ignored
* otherwise the line is printed

and pipes the output of the command to a udp socket

Config:

* command: command to run and pipe data to and from (defaul /usr/bin/env bash)
* serverhost: host to send to (default: localhost)
* serverport: port to send to (default: 7890)
* viewport: <width>x<height> in lines to set the viewport to. 80x24 as default
* env.XXX: will set the environment variable for command of XXX to the value
* auto-delay: delay when auto typing
