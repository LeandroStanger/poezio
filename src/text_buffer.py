# Copyright 2010-2011 Florent Le Coz <louiz@louiz.org>
#
# This file is part of Poezio.
#
# Poezio is free software: you can redistribute it and/or modify
# it under the terms of the zlib license. See the COPYING file.

"""
Define the TextBuffer class
"""

import logging
log = logging.getLogger(__name__)

import collections

from datetime import datetime
from config import config
from theming import get_theme

message_fields = 'txt nick_color time str_time nickname user identifier highlight me old_message revisions'

class Message(object):
    __slots__ = ('txt', 'nick_color', 'time', 'str_time', 'nickname', 'user',
                 'identifier', 'highlight', 'me', 'old_message', 'revisions')
    #'txt nick_color time str_time nickname user identifier highlight me old_message revisions'
    def __init__(self, txt, nick_color, time, str_time, nickname, user, identifier, highlight, me, old_message, revisions):
        self.txt = txt
        self.nick_color = nick_color
        self.time = time
        self.str_time = str_time
        self.nickname = nickname
        self.user = user
        self.identifier = identifier
        self.highlight = highlight
        self.me = me
        self.old_message = old_message
        self.revisions = revisions

    @classmethod
    def other_elems(obj):
        acc = ['Message(']
        fields = message_fields.split()
        fields.remove('old_message')
        for field in fields:
            acc.append('%s=%s' % (field, getattr(self, field)))
        return (', '.join(acc) + ', old_message=')

    def __repr__(self):
        init = other_elems(self)
        acc = []
        next = self.old_message
        rev = 0
        while next:
            acc.append(self.other_elems(next))
            next = next.old_message
            rev += 1
        acc.append('None')
        while rev:
            acc.append(')')
            rev -= 1
        return ''.join(acc)

    __str__ = __repr__


class CorrectionError(Exception): pass

class TextBuffer(object):
    """
    This class just keep trace of messages, in a list with various
    informations and attributes.
    """
    def __init__(self, messages_nb_limit=config.get('max_messages_in_memory', 2048)):
        self.messages_nb_limit = messages_nb_limit
        self.messages = []         # Message objects
        self.windows = []       # we keep track of one or more windows
        # so we can pass the new messages to them, as they are added, so
        # they (the windows) can build the lines from the new message

    def add_window(self, win):
        self.windows.append(win)

    @property
    def last_message(self):
        return self.messages[-1] if self.messages else None


    def make_message(self, txt, time, nickname, nick_color, history, user, identifier, str_time=None, highlight=False, old_message=None, revisions=0):
        time = time or datetime.now()
        me = False
        if txt.startswith('/me '):
            me = True
            txt = '\x19%(info_col)s}' % {'info_col': get_theme().COLOR_ME_MESSAGE[0]} + txt[4:]
        msg = Message(
                txt='%s\x19o'%(txt.replace('\t', '    '),),
                nick_color=nick_color,
                time=time,
                str_time=(time.strftime("%Y-%m-%d %H:%M:%S") if history else time.strftime("%H:%M:%S")) if str_time is None else '',
                nickname=nickname,
                user=user,
                identifier=identifier,
                highlight=highlight,
                me=me,
                old_message=old_message,
                revisions=revisions)
        log.debug('Set message %s with %s.', identifier, msg)
        return msg

    def add_message(self, txt, time=None, nickname=None, nick_color=None, history=None, user=None, highlight=False, identifier=None, str_time=None):
        msg = self.make_message(txt, time, nickname, nick_color, history, user, identifier, str_time=str_time, highlight=highlight)
        self.messages.append(msg)
        while len(self.messages) > self.messages_nb_limit:
            self.messages.pop(0)
        ret_val = None
        for window in self.windows: # make the associated windows
            # build the lines from the new message
            nb = window.build_new_message(msg, history=history, highlight=highlight, timestamp=config.get("show_timestamps", "true") != 'false')
            if ret_val is None:
                ret_val = nb
            if window.pos != 0:
                window.scroll_up(nb)
        return ret_val or 1

    def modify_message(self, txt, old_id, new_id, highlight=False, time=None, user=None):
        for i in range(len(self.messages) -1, -1, -1):
            msg = self.messages[i]
            if msg.identifier == old_id:
                if msg.user and msg.user is not user:
                    raise CorrectionError("wrong user")
                elif len(msg.str_time) > 8: # ugly
                    raise CorrectionError("delayed message")
                message = self.make_message(txt, time if time else msg.time, msg.nickname, msg.nick_color, None, msg.user, new_id, highlight=highlight, old_message=msg, revisions=msg.revisions + 1)
                self.messages[i] = message
                log.debug('Replacing message %s with %s.', old_id, new_id)
                return message
        log.debug('Message %s not found in text_buffer, abort replacement.', old_id)
        raise CorrectionError("nothing to replace")

    def del_window(self, win):
        self.windows.remove(win)

    def __del__(self):
        log.debug('** Deleting %s messages from textbuffer', len(self.messages))
