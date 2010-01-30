#!/usr/bin/python
# -*- coding:utf-8 -*-
#
# Copyright 2010 Le Coz Florent <louizatakk@fedoraproject.org>
#
# This file is part of Poezio.
#
# Poezio is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# Poezio is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Poezio.  If not, see <http://www.gnu.org/licenses/>.

from handler import Handler
import curses
from curses import textpad

import locale
from datetime import datetime

from logging import logger

from random import randrange

from config import config

locale.setlocale(locale.LC_ALL, '')
code = locale.getpreferredencoding()

import sys

from connection import *
from window import Window

class User(object):
    """
    keep trace of an user in a Room
    """
    def __init__(self, nick, affiliation, show, status, role):
        self.update(affiliation, show, status, role)
        self.change_nick(nick)
        self.color = randrange(2, 10)

    def update(self, affiliation, show, status, role):
        self.affiliation = None
        self.show = None
        self.status = status
        self.role = role

    def change_nick(self, nick):
        self.nick = nick.encode('utf-8')

class Room(object):
    """
    """
    def __init__(self, name, nick):
        self.name = name
        self.own_nick = nick
        self.joined = False     # false until self presence is received
        self.users = []
        self.lines = []         # (time, nick, msg) or (time, info)
        self.topic = ''

    def disconnect(self):
        self.joined = False
        self.users = []

    def add_message(self, nick, msg):
        if not msg:
            logger.info('msg is None..., %s' % (nick))
            return
        self.lines.append((datetime.now(), nick.encode('utf-8'), msg.encode('utf-8')))

    def add_info(self, info):
        """ info, like join/quit/status messages"""
        self.lines.append((datetime.now(), info.encode('utf-8')))
        return info.encode('utf-8')

    def get_user_by_name(self, nick):
        fd = open('fion', 'w')
        fd.write(nick)
        # fd.write('Looking for %s\n' % nick)
        for user in self.users:
            # fd.write(user.nick)
            if user.nick == nick:
                return user
        return None

    def on_presence(self, stanza, nick):
        """
        """
        affiliation = stanza.getAffiliation()
        show = stanza.getShow()
        status = stanza.getStatus()
        role = stanza.getRole()
        if not self.joined:     # user in the room BEFORE us.
             self.users.append(User(nick, affiliation, show, status, role))
             if nick.encode('utf-8') == self.own_nick:
                 self.joined = True
             return self.add_info("%s is in the room" % (nick))
        change_nick = stanza.getStatusCode() == '303'
        kick = stanza.getStatusCode() == '307'
        user = self.get_user_by_name(nick)
        # New user
        if not user:
            self.users.append(User(nick, affiliation, show, status, role))
            return self.add_info('%s joined the room %s' % (nick, self.name))
        # nick change
        if change_nick:
            if user.nick == self.own_nick:
                self.own_nick = stanza.getNick().encode('utf-8')
            user.change_nick(stanza.getNick())
            return self.add_info('%s is now known as %s' % (nick, stanza.getNick()))
        # kick
        if kick:
            self.users.remove(user)
            reason = stanza.getReason().encode('utf-8') or ''
            try:
                by = stanza.getActor().encode('utf-8')
            except:
                by = None
            if nick == self.own_nick:
                self.disconnect()
                if by:
                    return self.add_info('You have been kicked by %s. Reason: %s' % (by, reason))
                else:
                    return self.add_info('You have been kicked. Reason: %s' % (reason))
            else:
                if by:
                    return self.add_info('%s has been kicked by %s. Reason: %s' % (nick, by, reason))
                else:
                    return self.add_info('%s has been kicked. Reason: %s' % (nick, reason))
        # user quit
        if status == 'offline' or role == 'none':
            self.users.remove(user)
            return self.add_info('%s has left the room' % (nick))
        # status change
        user.update(affiliation, show, status, role)
        return self.add_info('%s, status : %s, %s, %s, %s' % (nick, affiliation, role, show, status))


class Gui(object):
    """
    Graphical user interface using ncurses
    """
    def __init__(self, stdscr=None, muc=None):

        self.init_curses(stdscr)
        self.stdscr = stdscr
        self.stdscr.leaveok(1)
        self.rooms = [Room('Info', '')]         # current_room is self.rooms[0]
        self.window = Window(stdscr)
        self.window.text_win.new_win('Info')
        self.window.refresh(self.rooms[0])

        self.muc = muc

        self.commands = {
            'join': self.command_join,
            'quit': self.command_quit,
            'next': self.rotate_rooms_left,
            'prev': self.rotate_rooms_right,
            'part': self.command_part,
            'nick': self.command_nick
            }

        self.key_func = {
            "KEY_LEFT": self.window.input.key_left,
            "KEY_RIGHT": self.window.input.key_right,
            "KEY_UP": self.window.input.key_up,
            "KEY_END": self.window.input.key_end,
            "KEY_HOME": self.window.input.key_home,
            "KEY_DOWN": self.window.input.key_down,
            "KEY_DC": self.window.input.key_dc,
            "KEY_F(5)": self.rotate_rooms_left,
            "KEY_F(6)": self.rotate_rooms_right,
            "kLFT5": self.rotate_rooms_left,
            "kRIT5": self.rotate_rooms_right,
            "KEY_BACKSPACE": self.window.input.key_backspace
            }

        self.handler = Handler()
        self.handler.connect('on-connected', self.on_connected)
        self.handler.connect('join-room', self.join_room)
        self.handler.connect('room-presence', self.room_presence)
        self.handler.connect('room-message', self.room_message)
        self.handler.connect('room-iq', self.room_iq)

    def main_loop(self, stdscr):
        while 1:
            curses.doupdate()
            key = stdscr.getkey()
            # print key
            # sys.exit()
            if str(key) in self.key_func.keys():
                self.key_func[key]()
            elif len(key) >= 4:
                continue
            elif ord(key) == 10:
                self.execute()
            elif ord(key) == 8 or ord(key) == 127:
                self.window.input.key_backspace()
            elif ord(key) < 32:
                continue
            else:
                if ord(key) == 27 and ord(stdscr.getkey()) == 91:
                    last = ord(stdscr.getkey()) # FIXME: ugly ugly workaroung.
                    if last == 51:
                        self.window.input.key_dc()
                    continue
                elif ord(key) > 190 and ord(key) < 225:
                    key = key+stdscr.getkey()
                elif ord(key) == 226:
                    key = key+stdscr.getkey()
                    key = key+stdscr.getkey()
                self.window.do_command(key)

    def current_room(self):
	return self.rooms[0]

    def get_room_by_name(self, name):
	for room in self.rooms:
	    if room.name == name:
		return room
	return None

    def init_curses(self, stdscr):
        curses.start_color()
        curses.noecho()
        stdscr.keypad(True)
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(2, curses.COLOR_BLUE, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK) # Admin
        curses.init_pair(4, curses.COLOR_BLUE, curses.COLOR_BLACK) # Participant
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLACK) # Visitor
        curses.init_pair(6, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(7, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(8, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
        curses.init_pair(9, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    def reset_curses(self):
	curses.echo()
        curses.endwin()

    def on_connected(self, jid):
        self.information("Welcome on Poezio \o/ !")
        self.information("Your JID is %s" % jid)
        pass

    def join_room(self, room, nick):
        self.window.text_win.new_win(room)
        self.rooms.insert(0, Room(room, nick))
        self.window.refresh(self.current_room())

    def rotate_rooms_left(self, args=None):
        self.rooms.append(self.rooms.pop(0))
        self.window.refresh(self.current_room())

    def rotate_rooms_right(self, args=None):
        self.rooms.insert(0, self.rooms.pop())
        self.window.refresh(self.current_room())

    def room_message(self, stanza):
        if len(sys.argv) > 1:
            self.information(str(stanza))
        if stanza.getType() != 'groupchat':
            return  # ignore all messages not comming from a MUC
        room_from = stanza.getFrom().getStripped()
        nick_from = stanza.getFrom().getResource()
        if not nick_from:
            nick_from = ''
	room = self.get_room_by_name(room_from)
	if not room:
	    self.information("message received for a non-existing room: %s" % (name))
            return
        body = stanza.getBody()
        if not body:
            body = stanza.getSubject()
            info = room.add_info("%s changed the subject to: %s" % (nick_from, stanza.getSubject()))
            self.window.text_win.add_line(room, (datetime.now(), info))
            room.topic = stanza.getSubject().encode('utf-8').replace('\n', '|')
            if room == self.current_room():
                self.window.topic_win.refresh(room.topic)
            curses.doupdate()
        else:
            room.add_message(nick_from, body)
            self.window.text_win.add_line(room, (datetime.now(), nick_from.encode('utf-8'), body.encode('utf-8')))
        if room == self.current_room():
            self.window.text_win.refresh(room.name)
            self.window.input.refresh()
        curses.doupdate()

    def room_presence(self, stanza):
        if len(sys.argv) > 1:
            self.information(str(stanza))
        from_nick = stanza.getFrom().getResource()
        from_room = stanza.getFrom().getStripped()
	room = self.get_room_by_name(from_room)
	if not room:
	    self.information("presence received for a non-existing room: %s" % (name))
        msg = room.on_presence(stanza, from_nick)
        if room == self.current_room():
            self.window.text_win.add_line(room, (datetime.now(), msg))
            self.window.text_win.refresh(room.name)
            self.window.user_win.refresh(room.users)
            self.window.text_win.refresh()
            curses.doupdate()

    def room_iq(self, iq):
        if len(sys.argv) > 1:
            self.information(str(iq))

    def execute(self):
        line = self.window.input.get_text()
        self.window.input.clear_text()
        self.window.input.refresh()
        curses.doupdate()
        if line == "":
            return
        if line.startswith('/'):
            command = line.strip()[:].split()[0][1:]
            args = line.strip()[:].split()[1:]
            if command in self.commands.keys():
                func = self.commands[command]
                func(args)
                return
        if self.current_room().name != 'Info':
            self.muc.send_message(self.current_room().name, line)
	self.window.input.refresh()

    def command_join(self, args):
        if len(args) == 0:
            r = self.current_room()
            if r.name == 'Info':
                return
            room = r.name
            nick = r.own_nick
        else:
            info = args[0].split('/')
            if len(info) == 1:
                nick = config.get('default_nick', 'Poezio')
            else:
                nick = info[1]
            if info[0] == '':   # happens with /join /nickname, wich is OK
                r = self.current_room()
                if r.name == 'Info':
                    return
                room = r.name
            else:
                room = info[0]
            r = self.get_room_by_name(room)
        if r and r.joined:                   # if we are already in the room
            self.information("already in room [%s]" % room)
            return
        self.muc.join_room(room, nick)
        if not r: # if the room window exists, we don't recreate it.
            self.join_room(room, nick)

    def command_part(self, args):
        reason = None
        room = self.current_room()
        if room.name == 'Info':
            return
        if len(args):
            msg = ' '.join(args)
        else:
            msg = None
        self.muc.quit_room(room.name, room.own_nick, msg)
        self.rooms.remove(self.current_room())
        self.window.refresh(self.current_room())

    def command_nick(self, args):
        if len(args) != 1:
            return
        nick = args[0]
        room = self.current_room()
        if not room.joined or room.name == "Info":
            return
        self.muc.change_nick(room.name, nick)

    def information(self, msg):
        room = self.get_room_by_name("Info")
        info = room.add_info(msg)
        if self.current_room() == room:
            self.window.text_win.add_line(room, (datetime.now(), info))
            self.window.text_win.refresh(room.name)
            curses.doupdate()

    def command_quit(self, args):
	self.reset_curses()
        sys.exit()
