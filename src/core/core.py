"""
Module defining the Core class, which is the central orchestrator
of poezio and contains the main loop, the list of tabs, sets the state
of everything; it also contains global commands, completions and event
handlers but those are defined in submodules in order to avoir cluttering
this file.
"""
import logging

log = logging.getLogger(__name__)

import collections
import curses
import os
import pipes
import sys
import time
from threading import Event
from datetime import datetime
from gettext import gettext as _

from sleekxmpp.xmlstream.handler import Callback

import bookmark
import connection
import decorators
import events
import singleton
import tabs
import theming
import timed_events
import windows

from common import safeJID
from config import config, firstrun
from contact import Contact, Resource
from daemon import Executor
from data_forms import DataFormsTab
from fifo import Fifo
from keyboard import keyboard
from logger import logger
from plugin_manager import PluginManager
from roster import roster
from text_buffer import TextBuffer
from theming import get_theme
from windows import g_lock

from . import completions
from . import commands
from . import handlers
from . structs import possible_show, DEPRECATED_ERRORS, \
        ERROR_AND_STATUS_CODES, Command, Status


class Core(object):
    """
    “Main” class of poezion
    """

    def __init__(self):
        # All uncaught exception are given to this callback, instead
        # of being displayed on the screen and exiting the program.
        sys.excepthook = self.on_exception
        self.connection_time = time.time()
        status = config.get('status', None)
        status = possible_show.get(status, None)
        self.status = Status(show=status,
                message=config.get('status_message', ''))
        self.running = True
        self.xmpp = singleton.Singleton(connection.Connection)
        self.xmpp.core = self
        roster.set_node(self.xmpp.client_roster)
        decorators.refresh_wrapper.core = self
        self.paused = False
        self.event = Event()
        self.debug = False
        self.remote_fifo = None
        # a unique buffer used to store global informations
        # that are displayed in almost all tabs, in an
        # information window.
        self.information_buffer = TextBuffer()
        self.information_win_size = config.get('info_win_height', 2, 'var')
        self.information_win = windows.TextWin(300)
        self.information_buffer.add_window(self.information_win)

        self.tab_win = windows.GlobalInfoBar()
        # Number of xml tabs opened, used to avoid useless memory consumption
        self.xml_tab = False
        self.xml_buffer = TextBuffer()

        self.tabs = []
        self._current_tab_nb = 0
        self.previous_tab_nb = 0

        self.own_nick = config.get('default_nick', '') or self.xmpp.boundjid.user or os.environ.get('USER') or 'poezio'

        self.plugins_autoloaded = False
        self.plugin_manager = PluginManager(self)
        self.events = events.EventHandler()


        # global commands, available from all tabs
        # a command is tuple of the form:
        # (the function executing the command. Takes a string as argument,
        #  a string representing the help message,
        #  a completion function, taking a Input as argument. Can be None)
        #  The completion function should return True if a completion was
        #  made ; False otherwise
        self.commands = {}
        self.register_initial_commands()

        # We are invisible
        if not config.get('send_initial_presence', True):
            del self.commands['status']
            del self.commands['show']

        self.key_func = KeyDict()
        # Key bindings associated with handlers
        # and pseudo-keys used to map actions below.
        key_func = {
            "KEY_PPAGE": self.scroll_page_up,
            "KEY_NPAGE": self.scroll_page_down,
            "^B": self.scroll_line_up,
            "^F": self.scroll_line_down,
            "^X": self.scroll_half_down,
            "^S": self.scroll_half_up,
            "KEY_F(5)": self.rotate_rooms_left,
            "^P": self.rotate_rooms_left,
            "M-[-D": self.rotate_rooms_left,
            'kLFT3': self.rotate_rooms_left,
            "KEY_F(6)": self.rotate_rooms_right,
            "^N": self.rotate_rooms_right,
            "M-[-C": self.rotate_rooms_right,
            'kRIT3': self.rotate_rooms_right,
            "KEY_F(4)": self.toggle_left_pane,
            "KEY_F(7)": self.shrink_information_win,
            "KEY_F(8)": self.grow_information_win,
            "KEY_RESIZE": self.call_for_resize,
            'M-e': self.go_to_important_room,
            'M-r': self.go_to_roster,
            'M-z': self.go_to_previous_tab,
            '^L': self.full_screen_redraw,
            'M-j': self.go_to_room_number,
            'M-D': self.scroll_info_up,
            'M-C': self.scroll_info_down,
            'M-k': self.escape_next_key,
        ######## actions mappings ##########
            '_bookmark': self.command_bookmark,
            '_bookmark_local': self.command_bookmark_local,
            '_close_tab': self.close_tab,
            '_disconnect': self.disconnect,
            '_quit': self.command_quit,
            '_redraw_screen': self.full_screen_redraw,
            '_reload_theme': self.command_theme,
            '_remove_bookmark': self.command_remove_bookmark,
            '_room_left': self.rotate_rooms_left,
            '_room_right': self.rotate_rooms_right,
            '_show_roster': self.go_to_roster,
            '_scroll_down': self.scroll_page_down,
            '_scroll_up': self.scroll_page_up,
            '_scroll_info_up': self.scroll_info_up,
            '_scroll_info_down': self.scroll_info_down,
            '_server_cycle': self.command_server_cycle,
            '_show_bookmarks': self.command_bookmarks,
            '_show_important_room': self.go_to_important_room,
            '_show_invitations': self.command_invitations,
            '_show_plugins': self.command_plugins,
            '_show_xmltab': self.command_xml_tab,
            '_toggle_pane': self.toggle_left_pane,
        ###### status actions ######
            '_available': lambda: self.command_status('available'),
            '_away': lambda: self.command_status('away'),
            '_chat': lambda: self.command_status('chat'),
            '_dnd': lambda: self.command_status('dnd'),
            '_xa': lambda: self.command_status('xa'),
        ##### Custom actions ########
            '_exc_': lambda arg: self.try_execute(arg),
        }
        self.key_func.update(key_func)

        # Add handlers
        self.xmpp.add_event_handler('connected', self.on_connected)
        self.xmpp.add_event_handler('disconnected', self.on_disconnected)
        self.xmpp.add_event_handler('failed_auth', self.on_failed_auth)
        self.xmpp.add_event_handler('no_auth', self.on_no_auth)
        self.xmpp.add_event_handler("session_start", self.on_session_start)
        self.xmpp.add_event_handler("session_start", self.on_session_start_features)
        self.xmpp.add_event_handler("groupchat_presence", self.on_groupchat_presence)
        self.xmpp.add_event_handler("groupchat_message", self.on_groupchat_message)
        self.xmpp.add_event_handler("groupchat_invite", self.on_groupchat_invite)
        self.xmpp.add_event_handler("groupchat_decline", self.on_groupchat_decline)
        self.xmpp.add_event_handler("groupchat_config_status", self.on_status_codes)
        self.xmpp.add_event_handler("groupchat_subject", self.on_groupchat_subject)
        self.xmpp.add_event_handler("message", self.on_message)
        self.xmpp.add_event_handler("got_online" , self.on_got_online)
        self.xmpp.add_event_handler("got_offline" , self.on_got_offline)
        self.xmpp.add_event_handler("roster_update", self.on_roster_update)
        self.xmpp.add_event_handler("changed_status", self.on_presence)
        self.xmpp.add_event_handler("presence_error", self.on_presence_error)
        self.xmpp.add_event_handler("roster_subscription_request", self.on_subscription_request)
        self.xmpp.add_event_handler("roster_subscription_authorized", self.on_subscription_authorized)
        self.xmpp.add_event_handler("roster_subscription_remove", self.on_subscription_remove)
        self.xmpp.add_event_handler("roster_subscription_removed", self.on_subscription_removed)
        self.xmpp.add_event_handler("message_xform", self.on_data_form)
        self.xmpp.add_event_handler("chatstate_active", self.on_chatstate_active)
        self.xmpp.add_event_handler("chatstate_composing", self.on_chatstate_composing)
        self.xmpp.add_event_handler("chatstate_paused", self.on_chatstate_paused)
        self.xmpp.add_event_handler("chatstate_gone", self.on_chatstate_gone)
        self.xmpp.add_event_handler("chatstate_inactive", self.on_chatstate_inactive)
        self.xmpp.add_event_handler("attention", self.on_attention)
        self.xmpp.add_event_handler("ssl_cert", self.validate_ssl)
        self.all_stanzas = Callback('custom matcher', connection.MatchAll(None), self.incoming_stanza)
        self.xmpp.register_handler(self.all_stanzas)
        if config.get('enable_user_tune', True):
            self.xmpp.add_event_handler("user_tune_publish", self.on_tune_event)
        if config.get('enable_user_nick', True):
            self.xmpp.add_event_handler("user_nick_publish", self.on_nick_received)
        if config.get('enable_user_mood', True):
            self.xmpp.add_event_handler("user_mood_publish", self.on_mood_event)
        if config.get('enable_user_activity', True):
            self.xmpp.add_event_handler("user_activity_publish", self.on_activity_event)
        if config.get('enable_user_gaming', True):
            self.xmpp.add_event_handler("user_gaming_publish", self.on_gaming_event)

        self.initial_joins = []

        self.timed_events = set()

        self.connected_events = {}

        self.pending_invites = {}

        # a dict of the form {'config_option': [list, of, callbacks]}
        # Whenever a configuration option is changed (using /set or by
        # reloading a new config using a signal), all the associated
        # callbacks are triggered.
        # Use Core.add_configuration_handler("option", callback) to add a
        # handler
        # Note that the callback will be called when it’s changed in the global section, OR
        # in a special section.
        # As a special case, handlers can be associated with the empty
        # string option (""), they will be called for every option change
        # The callback takes two argument: the config option, and the new
        # value
        self.configuration_change_handlers = {"": []}
        self.add_configuration_handler("create_gaps", self.on_gaps_config_change)
        self.add_configuration_handler("plugins_dir", self.on_plugins_dir_config_change)
        self.add_configuration_handler("plugins_conf_dir", self.on_plugins_conf_dir_config_change)
        self.add_configuration_handler("connection_timeout_delay", self.xmpp.set_keepalive_values)
        self.add_configuration_handler("connection_check_interval", self.xmpp.set_keepalive_values)
        self.add_configuration_handler("themes_dir", theming.update_themes_dir)
        self.add_configuration_handler("", self.on_any_config_change)

    def on_any_config_change(self, option, value):
        """
        Update the roster, in case a roster option changed.
        """
        roster.modified()

    def add_configuration_handler(self, option, callback):
        """
        Add a callback, associated with the given option. It will be called
        each time the configuration option is changed using /set or by
        reloading the configuration with a signal
        """
        if option not in self.configuration_change_handlers:
            self.configuration_change_handlers[option] = []
        self.configuration_change_handlers[option].append(callback)

    def trigger_configuration_change(self, option, value):
        """
        Triggers all the handlers associated with the given configuration
        option
        """
        # First call the callbacks associated with any configuration change
        for callback in self.configuration_change_handlers[""]:
            callback(option, value)
        # and then the callbacks associated with this specific option, if
        # any
        if option not in self.configuration_change_handlers:
            return
        for callback in self.configuration_change_handlers[option]:
            callback(option, value)

    def on_gaps_config_change(self, option, value):
        """
        Called when the option create_gaps is changed.
        Remove all gaptabs if switching from gaps to nogaps.
        """
        if value.lower() == "false":
            self.tabs = list(filter(lambda x: bool(x), self.tabs))

    def on_plugins_dir_config_change(self, option, value):
        """
        Called when the plugins_dir option is changed
        """
        path = os.path.expanduser(value)
        self.plugin_manager.on_plugins_dir_change(path)

    def on_plugins_conf_dir_config_change(self, option, value):
        """
        Called when the plugins_conf_dir option is changed
        """
        path = os.path.expanduser(value)
        self.plugin_manager.on_plugins_conf_dir_change(path)

    def sigusr_handler(self, num, stack):
        """
        Handle SIGUSR1 (10)
        When caught, reload all the possible files.
        """
        log.debug("SIGUSR1 caught, reloading the files…")
        # reload all log files
        log.debug("Reloading the log files…")
        logger.reload_all()
        log.debug("Log files reloaded.")
        # reload the theme
        log.debug("Reloading the theme…")
        self.command_theme("")
        log.debug("Theme reloaded.")
        # reload the config from the disk
        log.debug("Reloading the config…")
        # Copy the old config in a dict
        old_config = config.to_dict()
        config.read_file(config.file_name)
        # Compare old and current config, to trigger the callbacks of all
        # modified options
        for section in config.sections():
            old_section = old_config.get(section, {})
            for option in config.options(section):
                old_value = old_section.get(option)
                new_value = config.get(option, "", section)
                if new_value != old_value:
                    self.trigger_configuration_change(option, new_value)
        log.debug("Config reloaded.")
        # in case some roster options have changed
        roster.modified()

    def exit_from_signal(self, *args, **kwargs):
        """
        Quit when receiving SIGHUP or SIGTERM

        do not save the config because it is not a normal exit
        (and only roster UI things are not yet saved)
        """
        log.debug("Either SIGHUP or SIGTERM received. Exiting…")
        if config.get('enable_user_mood', True):
            self.xmpp.plugin['xep_0107'].stop(block=False)
        if config.get('enable_user_activity', True):
            self.xmpp.plugin['xep_0108'].stop(block=False)
        if config.get('enable_user_gaming', True):
            self.xmpp.plugin['xep_0196'].stop(block=False)
        self.plugin_manager.disable_plugins()
        self.disconnect('')
        self.running = False
        try:
            self.reset_curses()
        except: # too bad
            pass
        sys.exit()

    def autoload_plugins(self):
        """
        Load the plugins on startup.
        """
        plugins = config.get('plugins_autoload', '')
        if ':' in plugins:
            for plugin in plugins.split(':'):
                self.plugin_manager.load(plugin)
        else:
            for plugin in plugins.split():
                self.plugin_manager.load(plugin)
        self.plugins_autoloaded = True

    def start(self):
        """
        Init curses, create the first tab, etc
        """
        self.stdscr = curses.initscr()
        self.init_curses(self.stdscr)
        self.call_for_resize()
        default_tab = tabs.RosterInfoTab()
        default_tab.on_gain_focus()
        self.tabs.append(default_tab)
        self.information(_('Welcome to poezio!'))
        if firstrun:
            self.information(_(
                'It seems that it is the first time you start poezio.\n'
                'The online help is here http://poezio.eu/doc/en/\n'
                'No room is joined by default, but you can join poezio’s chatroom '
                '(with /join poezio@muc.poezio.eu), where you can ask for help or tell us how great it is.'
            ), 'Help')
        self.refresh_window()

    def on_exception(self, typ, value, trace):
        """
        When an exception is raised, just reset curses and call
        the original exception handler (will nicely print the traceback)
        """
        try:
            self.reset_curses()
        except:
            pass
        sys.__excepthook__(typ, value, trace)

    def main_loop(self):
        """
        main loop waiting for the user to press a key
        """
        def replace_line_breaks(key):
            if key == '^J':
                return '\n'
            return key
        def separate_chars_from_bindings(char_list):
            """
            returns a list of lists. For example if you give
            ['a', 'b', 'KEY_BACKSPACE', 'n', 'u'], this function returns
            [['a', 'b'], ['KEY_BACKSPACE'], ['n', 'u']]

            This way, in case of lag (for example), we handle the typed text
            by “batch” as much as possible (instead of one char at a time,
            which implies a refresh after each char, which is very slow),
            but we still handle the special chars (backspaces, arrows,
            ctrl+x ou alt+x, etc) one by one, which avoids the issue of
            printing them OR ignoring them in that case.  This should
            resolve the “my ^W are ignored when I lag ;(”.
            """
            res = []
            current = []
            for char in char_list:
                assert(len(char) > 0)
                # Transform that stupid char into what we actually meant
                if char == '\x1f':
                    char = '^/'
                if len(char) == 1:
                    current.append(char)
                else:
                    # special case for the ^I key, it’s considered as \t
                    # only when pasting some text, otherwise that’s the ^I
                    # (or M-i) key, which stands for completion by default.
                    if char == '^I' and len(char_list) != 1:
                        current.append('\t')
                        continue
                    if current:
                        res.append(current)
                        current = []
                    res.append([char])
            if current:
                res.append(current)
            return res

        while self.running:
            self.xmpp.plugin['xep_0012'].begin_idle(jid=self.xmpp.boundjid)
            big_char_list = [replace_key_with_bound(key)\
                             for key in self.read_keyboard()]
            # whether to refresh after ALL keys have been handled
            for char_list in separate_chars_from_bindings(big_char_list):
                if self.paused:
                    self.current_tab().input.do_command(char_list[0])
                    self.current_tab().input.prompt()
                    self.event.set()
                    continue
                # Special case for M-x where x is a number
                if len(char_list) == 1:
                    char = char_list[0]
                    if char.startswith('M-') and len(char) == 3:
                        try:
                            nb = int(char[2])
                        except ValueError:
                            pass
                        else:
                            if self.current_tab().nb == nb:
                                self.go_to_previous_tab()
                            else:
                                self.command_win('%d' % nb)
                        # search for keyboard shortcut
                    func = self.key_func.get(char, None)
                    if func:
                        func()
                    else:
                        res = self.do_command(replace_line_breaks(char), False)
                else:
                    self.do_command(''.join(char_list), True)
            self.doupdate()

    def save_config(self):
        """
        Save config in the file just before exit
        """
        if not roster.save_to_config_file() or \
                not config.silent_set('info_win_height', self.information_win_size, 'var'):
            self.information(_('Unable to write in the config file'), 'Error')

    def on_roster_enter_key(self, roster_row):
        """
        when enter is pressed on the roster window
        """
        if isinstance(roster_row, Contact):
            if not self.get_conversation_by_jid(roster_row.bare_jid, False):
                self.open_conversation_window(roster_row.bare_jid)
            else:
                self.focus_tab_named(roster_row.bare_jid)
        if isinstance(roster_row, Resource):
            if not self.get_conversation_by_jid(roster_row.jid, False, fallback_barejid=False):
                self.open_conversation_window(roster_row.jid)
            else:
                self.focus_tab_named(roster_row.jid)
        self.refresh_window()

    def get_conversation_messages(self):
        """
        Returns a list of all the messages in the current chat.
        If the current tab is not a ChatTab, returns None.

        Messages are namedtuples of the form
        ('txt nick_color time str_time nickname user')
        """
        if not isinstance(self.current_tab(), tabs.ChatTab):
            return None
        return self.current_tab().get_conversation_messages()

    def insert_input_text(self, text):
        """
        Insert the given text into the current input
        """
        self.do_command(text, True)


##################### Anything related to command execution ###################

    def execute(self, line):
        """
        Execute the /command or just send the line on the current room
        """
        if line == "":
            return
        if line.startswith('/'):
            command = line.strip()[:].split()[0][1:]
            arg = line[2+len(command):] # jump the '/' and the ' '
            # example. on "/link 0 open", command = "link" and arg = "0 open"
            if command in self.commands:
                func = self.commands[command][0]
                func(arg)
                return
            else:
                self.information(_("Unknown command (%s)") % (command), _('Error'))

    def exec_command(self, command):
        """
        Execute an external command on the local or a remote machine,
        depending on the conf. For example, to open a link in a browser, do
        exec_command(["firefox", "http://poezio.eu"]), and this will call
        the command on the correct computer.

        The command argument is a list of strings, not quoted or escaped in
        any way. The escaping is done here if needed.

        The remote execution is done
        by writing the command on a fifo.  That fifo has to be on the
        machine where poezio is running, and accessible (through sshfs for
        example) from the local machine (where poezio is not running). A
        very simple daemon (daemon.py) reads on that fifo, and executes any
        command that is read in it. Since we can only write strings to that
        fifo, each argument has to be pipes.quote()d. That way the
        shlex.split on the reading-side of the daemon will be safe.

        You cannot use a real command line with pipes, redirections etc, but
        this function supports a simple case redirection to file: if the
        before-last argument of the command is ">" or ">>", then the last
        argument is considered to be a filename where the command stdout
        will be written. For example you can do exec_command(["echo",
        "coucou les amis coucou coucou", ">", "output.txt"]) and this will
        work. If you try to do anything else, your |, [, <<, etc will be
        interpreted as normal command arguments, not shell special tokens.
        """
        if config.get('exec_remote', False):
            # We just write the command in the fifo
            if not self.remote_fifo:
                try:
                    self.remote_fifo = Fifo(os.path.join(config.get('remote_fifo_path', './'), 'poezio.fifo'), 'w')
                except (OSError, IOError) as e:
                    log.error('Could not open the fifo for writing (%s)',
                            os.path.join(config.get('remote_fifo_path', './'), 'poezio.fifo'),
                            exc_info=True)
                    self.information('Could not open fifo file for writing: %s' % (e,), 'Error')
                    return
            command_str = ' '.join([pipes.quote(arg.replace('\n', ' ')) for arg in command]) + '\n'
            try:
                self.remote_fifo.write(command_str)
            except (IOError) as e:
                log.error('Could not write in the fifo (%s): %s',
                            os.path.join(config.get('remote_fifo_path', './'), 'poezio.fifo'),
                            repr(command),
                            exc_info=True)
                self.information('Could not execute %s: %s' % (command, e,), 'Error')
                self.remote_fifo = None
        else:
            e = Executor(command)
            try:
                e.start()
            except ValueError as e:
                log.error('Could not execute command (%s)', repr(command), exc_info=True)
                self.information('%s' % (e,), 'Error')


    def do_command(self, key, raw):
        if not key:
            return
        return self.current_tab().on_input(key, raw)


    def try_execute(self, line):
        """
        Try to execute a command in the current tab
        """
        line = '/' + line
        try:
            self.current_tab().execute_command(line)
        except:
            log.error('Execute failed (%s)', line, exc_info=True)


########################## TImed Events #######################################

    def remove_timed_event(self, event):
        """Remove an existing timed event"""
        if event and event in self.timed_events:
            self.timed_events.remove(event)

    def add_timed_event(self, event):
        """Add a new timed event"""
        self.timed_events.add(event)

    def check_timed_events(self):
        """Check for the execution of timed events"""
        now = datetime.now()
        for event in self.timed_events:
            if event.has_timed_out(now):
                res = event()
                if not res:
                    self.timed_events.remove(event)
                    break


####################### XMPP-related actions ##################################

    def get_status(self):
        """
        Get the last status that was previously set
        """
        return self.status

    def set_status(self, pres, msg):
        """
        Set our current status so we can remember
        it and use it back when needed (for example to display it
        or to use it when joining a new muc)
        """
        self.status = Status(show=pres, message=msg)
        if config.get('save_status', True):
            if not config.silent_set('status', pres if pres else '') or \
                    not config.silent_set('status_message', msg.replace('\n', '|') if msg else ''):
                self.information(_('Unable to write in the config file'), 'Error')

    def get_bookmark_nickname(self, room_name):
        """
        Returns the nickname associated with a bookmark
        or the default nickname
        """
        bm = bookmark.get_by_jid(room_name)
        if bm:
            return bm.nick
        return self.own_nick

    def disconnect(self, msg='', reconnect=False):
        """
        Disconnect from remote server and correctly set the states of all
        parts of the client (for example, set the MucTabs as not joined, etc)
        """
        msg = msg or ''
        for tab in self.get_tabs(tabs.MucTab):
            tab.command_part(msg)
        self.xmpp.disconnect()
        if reconnect:
            self.xmpp.start()

    def send_message(self, msg):
        """
        Function to use in plugins to send a message in the current conversation.
        Returns False if the current tab is not a conversation tab
        """
        if not isinstance(self.current_tab(), tabs.ChatTab):
            return False
        self.current_tab().command_say(msg)
        return True

    def get_error_message(self, stanza, deprecated=False):
        """
        Takes a stanza of the form <message type='error'><error/></message>
        and return a well formed string containing the error informations
        """
        sender = stanza.attrib['from']
        msg = stanza['error']['type']
        condition = stanza['error']['condition']
        code = stanza['error']['code']
        body = stanza['error']['text']
        if not body:
            if deprecated:
                if code in DEPRECATED_ERRORS:
                    body = DEPRECATED_ERRORS[code]
                else:
                    body = condition or _('Unknown error')
            else:
                if code in ERROR_AND_STATUS_CODES:
                    body = ERROR_AND_STATUS_CODES[code]
                else:
                    body = condition or _('Unknown error')
        if code:
            message = _('%(from)s: %(code)s - %(msg)s: %(body)s') % {'from':sender, 'msg':msg, 'body':body, 'code':code}
        else:
            message = _('%(from)s: %(msg)s: %(body)s') % {'from':sender, 'msg':msg, 'body':body}
        return message


####################### Tab logic-related things ##############################

    ### Tab getters ###

    def get_tabs(self, cls=tabs.Tab):
        "Get all the tabs of a type"
        return filter(lambda tab: isinstance(tab, cls), self.tabs)

    def current_tab(self):
        """
        returns the current room, the one we are viewing
        """
        self.current_tab_nb = self.current_tab_nb
        return self.tabs[self.current_tab_nb]

    def get_conversation_by_jid(self, jid, create=True, fallback_barejid=True):
        """
        From a JID, get the tab containing the conversation with it.
        If none already exist, and create is "True", we create it
        and return it. Otherwise, we return None.

        If fallback_barejid is True, then this method will seek other
        tabs with the same barejid, instead of searching only by fulljid.
        """
        jid = safeJID(jid)
        # We first check if we have a static conversation opened with this precise resource
        conversation = self.get_tab_by_name(jid.full, tabs.StaticConversationTab)
        if jid.bare == jid.full and not conversation:
            conversation = self.get_tab_by_name(jid.full, tabs.DynamicConversationTab)

        if not conversation and fallback_barejid:
            # If not, we search for a conversation with the bare jid
            conversation = self.get_tab_by_name(jid.bare, tabs.DynamicConversationTab)
            if not conversation:
                if create:
                    # We create a dynamic conversation with the bare Jid if
                    # nothing was found (and we lock it to the resource
                    # later)
                    conversation = self.open_conversation_window(jid.bare, False)
                else:
                    conversation = None
        return conversation

    def get_tab_by_name(self, name, typ=None):
        """
        Get the tab with the given name.
        If typ is provided, return a tab of this type only
        """
        for tab in self.tabs:
            if tab.get_name() == name:
                if (typ and isinstance(tab, typ)) or\
                        not typ:
                    return tab
        return None

    def get_tab_by_number(self, number):
        if 0 <= number < len(self.tabs):
            return self.tabs[number]
        return None

    def add_tab(self, new_tab, focus=False):
        """
        Appends the new_tab in the tab list and
        focus it if focus==True
        """
        self.tabs.append(new_tab)
        if focus:
            self.command_win("%s" % new_tab.nb)

    def insert_tab_nogaps(self, old_pos, new_pos):
        """
        Move tabs without creating gaps
        old_pos: old position of the tab
        new_pos: desired position of the tab
        """
        tab = self.tabs[old_pos]
        if new_pos < old_pos:
            self.tabs.pop(old_pos)
            self.tabs.insert(new_pos, tab)
        elif new_pos > old_pos:
            self.tabs.insert(new_pos, tab)
            self.tabs.remove(tab)
        else:
            return False
        return True

    def insert_tab_gaps(self, old_pos, new_pos):
        """
        Move tabs and create gaps in the eventual remaining space
        old_pos: old position of the tab
        new_pos: desired position of the tab
        """
        tab = self.tabs[old_pos]
        target = None if new_pos >= len(self.tabs) else self.tabs[new_pos]
        if not target:
            if new_pos < len(self.tabs):
                self.tabs[new_pos], self.tabs[old_pos] = self.tabs[old_pos], tabs.GapTab()
            else:
                self.tabs.append(self.tabs[old_pos])
                self.tabs[old_pos] = tabs.GapTab()
        else:
            if new_pos > old_pos:
                self.tabs.insert(new_pos, tab)
                self.tabs[old_pos] = tabs.GapTab()
            elif new_pos < old_pos:
                self.tabs[old_pos] = tabs.GapTab()
                self.tabs.insert(new_pos, tab)
            else:
                return False
            i = self.tabs.index(tab)
            done = False
            # Remove the first Gap on the right in the list
            # in order to prevent global shifts when there is empty space
            while not done:
                i += 1
                if i >= len(self.tabs):
                    done = True
                elif not self.tabs[i]:
                    self.tabs.pop(i)
                    done = True
        # Remove the trailing gaps
        i = len(self.tabs) - 1
        while isinstance(self.tabs[i], tabs.GapTab):
            self.tabs.pop()
            i -= 1
        return True

    def insert_tab(self, old_pos, new_pos=99999):
        """
        Insert a tab at a position, changing the number of the following tabs
        returns False if it could not move the tab, True otherwise
        """
        if old_pos <= 0 or old_pos >= len(self.tabs):
            return False
        elif new_pos <= 0:
            return False
        elif new_pos ==old_pos:
            return False
        elif not self.tabs[old_pos]:
            return False
        if config.get('create_gaps', False):
            return self.insert_tab_gaps(old_pos, new_pos)
        return self.insert_tab_nogaps(old_pos, new_pos)

    ### Move actions (e.g. go to next room) ###

    def rotate_rooms_right(self, args=None):
        """
        rotate the rooms list to the right
        """
        self.current_tab().on_lose_focus()
        self.current_tab_nb += 1
        while not self.tabs[self.current_tab_nb]:
            self.current_tab_nb += 1
        self.current_tab().on_gain_focus()
        self.refresh_window()

    def rotate_rooms_left(self, args=None):
        """
        rotate the rooms list to the right
        """
        self.current_tab().on_lose_focus()
        self.current_tab_nb -= 1
        while not self.tabs[self.current_tab_nb]:
            self.current_tab_nb -= 1
        self.current_tab().on_gain_focus()
        self.refresh_window()

    def go_to_room_number(self):
        """
        Read 2 more chars and go to the tab
        with the given number
        """
        char = self.read_keyboard()[0]
        try:
            nb1 = int(char)
        except ValueError:
            return
        char = self.read_keyboard()[0]
        try:
            nb2 = int(char)
        except ValueError:
            return
        self.command_win('%s%s' % (nb1, nb2))

    def go_to_roster(self):
        self.command_win('0')

    def go_to_previous_tab(self):
        self.command_win('%s' % (self.previous_tab_nb,))

    def go_to_important_room(self):
        """
        Go to the next room with activity, in the order defined in the
        dict tabs.STATE_PRIORITY
        """
        # shortcut
        priority = tabs.STATE_PRIORITY
        tab_refs = {}
        # put all the active tabs in a dict of lists by state
        for tab in self.tabs:
            if not tab:
                continue
            if tab.state not in tab_refs:
                tab_refs[tab.state] = [tab]
            else:
                tab_refs[tab.state].append(tab)
        # sort the state by priority and remove those with negative priority
        states = sorted(tab_refs.keys(), key=(lambda x: priority.get(x, 0)), reverse=True)
        states = [state for state in states if priority.get(state, -1) >= 0]

        for state in states:
            for tab in tab_refs[state]:
                if tab.nb < self.current_tab_nb and tab_refs[state][-1].nb > self.current_tab_nb:
                    continue
                self.command_win('%s' % tab.nb)
                return
        return

    def focus_tab_named(self, tab_name, type_=None):
        """Returns True if it found a tab to focus on"""
        for tab in self.tabs:
            if tab.get_name() == tab_name:
                if (type_ and (isinstance(tab, type_))) or not type_:
                    self.command_win('%s' % (tab.nb,))
                return True
        return False

    @property
    def current_tab_nb(self):
        return self._current_tab_nb

    @current_tab_nb.setter
    def current_tab_nb(self, value):
        if value >= len(self.tabs):
            self._current_tab_nb = 0
        elif value < 0:
            self._current_tab_nb = len(self.tabs) - 1
        else:
            self._current_tab_nb = value

    ### Opening actions ###

    def open_conversation_window(self, jid, focus=True):
        """
        Open a new conversation tab and focus it if needed. If a resource is
        provided, we open a StaticConversationTab, else a
        DynamicConversationTab
        """
        if safeJID(jid).resource:
            new_tab = tabs.StaticConversationTab(jid)
        else:
            new_tab = tabs.DynamicConversationTab(jid)
        if not focus:
            new_tab.state = "private"
        self.add_tab(new_tab, focus)
        self.refresh_window()
        return new_tab

    def open_private_window(self, room_name, user_nick, focus=True):
        """
        Open a Private conversation in a MUC and focus if needed.
        """
        complete_jid = room_name+'/'+user_nick
        # if the room exists, focus it and return
        for tab in self.get_tabs(tabs.PrivateTab):
            if tab.get_name() == complete_jid:
                self.command_win('%s' % tab.nb)
                return tab
        # create the new tab
        tab = self.get_tab_by_name(room_name, tabs.MucTab)
        if not tab:
            return None
        new_tab = tabs.PrivateTab(complete_jid, tab.own_nick)
        if hasattr(tab, 'directed_presence'):
            new_tab.directed_presence = tab.directed_presence
        if not focus:
            new_tab.state = "private"
        # insert it in the tabs
        self.add_tab(new_tab, focus)
        self.refresh_window()
        tab.privates.append(new_tab)
        return new_tab

    def open_new_room(self, room, nick, focus=True):
        """
        Open a new tab.MucTab containing a muc Room, using the specified nick
        """
        new_tab = tabs.MucTab(room, nick)
        self.add_tab(new_tab, focus)
        self.refresh_window()

    def open_new_form(self, form, on_cancel, on_send, **kwargs):
        """
        Open a new tab containing the form
        The callback are called with the completed form as parameter in
        addition with kwargs
        """
        form_tab = DataFormsTab(form, on_cancel, on_send, kwargs)
        self.add_tab(form_tab, True)

    ### Modifying actions ###
    def rename_private_tabs(self, room_name, old_nick, new_nick):
        """
        Call this method when someone changes his/her nick in a MUC, this updates
        the name of all the opened private conversations with him/her
        """
        tab = self.get_tab_by_name('%s/%s' % (room_name, old_nick), tabs.PrivateTab)
        if tab:
            tab.rename_user(old_nick, new_nick)

    def on_user_left_private_conversation(self, room_name, nick, status_message):
        """
        The user left the MUC: add a message in the associated private conversation
        """
        tab = self.get_tab_by_name('%s/%s' % (room_name, nick), tabs.PrivateTab)
        if tab:
            tab.user_left(status_message, nick)

    def on_user_rejoined_private_conversation(self, room_name, nick):
        """
        The user joined a MUC: add a message in the associated private conversation
        """
        tab = self.get_tab_by_name('%s/%s' % (room_name, nick), tabs.PrivateTab)
        if tab:
            tab.user_rejoined(nick)

    def disable_private_tabs(self, room_name, reason='\x195}You left the chatroom\x193}'):
        """
        Disable private tabs when leaving a room
        """
        for tab in self.get_tabs(tabs.PrivateTab):
            if tab.get_name().startswith(room_name):
                tab.deactivate(reason=reason)

    def enable_private_tabs(self, room_name, reason='\x195}You joined the chatroom\x193}'):
        """
        Enable private tabs when joining a room
        """
        for tab in self.get_tabs(tabs.PrivateTab):
            if tab.get_name().startswith(room_name):
                tab.activate(reason=reason)

    def on_user_changed_status_in_private(self, jid, msg):
        tab = self.get_tab_by_name(jid)
        if tab: # display the message in private
            tab.add_message(msg, typ=2)

    def close_tab(self, tab=None):
        """
        Close the given tab. If None, close the current one
        """
        tab = tab or self.current_tab()
        if isinstance(tab, tabs.RosterInfoTab):
            return              # The tab 0 should NEVER be closed
        del tab.key_func      # Remove self references
        del tab.commands      # and make the object collectable
        tab.on_close()
        nb = tab.nb
        if config.get('create_gaps', False):
            if nb >= len(self.tabs) - 1:
                self.tabs.remove(tab)
                nb -= 1
                while not self.tabs[nb]: # remove the trailing gaps
                    self.tabs.pop()
                    nb -= 1
            else:
                self.tabs[nb] = tabs.GapTab()
        else:
            self.tabs.remove(tab)
        if tab and tab.get_name() in logger.fds:
            logger.fds[tab.get_name()].close()
            log.debug("Log file for %s closed.", tab.get_name())
            del logger.fds[tab.get_name()]
        if self.current_tab_nb >= len(self.tabs):
            self.current_tab_nb = len(self.tabs) - 1
        while not self.tabs[self.current_tab_nb]:
            self.current_tab_nb -= 1
        self.current_tab().on_gain_focus()
        self.refresh_window()
        import gc
        gc.collect()
        log.debug('___ Referrers of closing tab:\n%s\n______', gc.get_referrers(tab))
        del tab

    def add_information_message_to_conversation_tab(self, jid, msg):
        """
        Search for a ConversationTab with the given jid (full or bare), if yes, add
        the given message to it
        """
        tab = self.get_tab_by_name(jid, tabs.ConversationTab)
        if tab:
            tab.add_message(msg, typ=2)
            if self.current_tab() is tab:
                self.refresh_window()


####################### Curses and ui-related stuff ###########################

    def doupdate(self):
        if not self.running or self.background is True:
            return
        curses.doupdate()

    def information(self, msg, typ=''):
        """
        Displays an informational message in the "Info" buffer
        """
        filter_messages = config.get('filter_info_messages', '').split(':')
        for words in filter_messages:
            if words and words in msg:
                log.debug('Did not show the message:\n\t%s> %s', typ, msg)
                return False
        colors = get_theme().INFO_COLORS
        color = colors.get(typ.lower(), colors.get('default', None))
        nb_lines = self.information_buffer.add_message(msg, nickname=typ, nick_color=color)
        if isinstance(self.current_tab(), tabs.RosterInfoTab):
            self.refresh_window()
        elif typ != '' and typ.lower() in config.get('information_buffer_popup_on',
                                           'error roster warning help info').split():
            popup_time = config.get('popup_time', 4) + (nb_lines - 1) * 2
            self.pop_information_win_up(nb_lines, popup_time)
        else:
            if self.information_win_size != 0:
                self.information_win.refresh()
                self.current_tab().input.refresh()
        return True

    def init_curses(self, stdscr):
        """
        ncurses initialization
        """
        self.background = False  # Bool to know if curses can draw
        # or be quiet while an other console app is running.
        curses.curs_set(1)
        curses.noecho()
        curses.nonl()
        curses.raw()
        stdscr.idlok(1)
        stdscr.keypad(1)
        curses.start_color()
        curses.use_default_colors()
        theming.reload_theme()
        curses.ungetch(" ")    # H4X: without this, the screen is
        stdscr.getkey()        # erased on the first "getkey()"

    def reset_curses(self):
        """
        Reset terminal capabilities to what they were before ncurses
        init
        """
        curses.echo()
        curses.nocbreak()
        curses.curs_set(1)
        curses.endwin()

    @property
    def informations(self):
        return self.information_buffer

    def refresh_window(self):
        """
        Refresh everything
        """
        self.current_tab().state = 'current'
        self.current_tab().refresh()
        self.doupdate()

    def refresh_tab_win(self):
        """
        Refresh the window containing the tab list
        """
        self.current_tab().refresh_tab_win()
        if self.current_tab().input:
            self.current_tab().input.refresh()
        self.doupdate()

    def scroll_page_down(self, args=None):
        """
        Scroll a page down, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_scroll_down():
            self.refresh_window()
            return True

    def scroll_page_up(self, args=None):
        """
        Scroll a page up, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_scroll_up():
            self.refresh_window()
            return True

    def scroll_line_up(self, args=None):
        """
        Scroll a line up, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_line_up():
            self.refresh_window()
            return True

    def scroll_line_down(self, args=None):
        """
        Scroll a line down, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_line_down():
            self.refresh_window()
            return True

    def scroll_half_up(self, args=None):
        """
        Scroll half a screen down, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_half_scroll_up():
            self.refresh_window()
            return True

    def scroll_half_down(self, args=None):
        """
        Scroll half a screen down, if possible.
        Returns True on success, None on failure.
        """
        if self.current_tab().on_half_scroll_down():
            self.refresh_window()
            return True

    def grow_information_win(self, nb=1):
        if self.information_win_size >= self.current_tab().height -5 or \
                self.information_win_size+nb >= self.current_tab().height-4:
            return 0
        if self.information_win_size == 14:
            return 0
        self.information_win_size += nb
        if self.information_win_size > 14:
            nb = nb - (self.information_win_size - 14)
            self.information_win_size = 14
        self.resize_global_information_win()
        for tab in self.tabs:
            tab.on_info_win_size_changed()
        self.refresh_window()
        return nb

    def shrink_information_win(self, nb=1):
        if self.information_win_size == 0:
            return
        self.information_win_size -= nb
        if self.information_win_size < 0:
            self.information_win_size = 0
        self.resize_global_information_win()
        for tab in self.tabs:
            tab.on_info_win_size_changed()
        self.refresh_window()

    def scroll_info_up(self):
        self.information_win.scroll_up(self.information_win.height)
        if not isinstance(self.current_tab(), tabs.RosterInfoTab):
            self.information_win.refresh()
        else:
            info = self.current_tab().information_win
            info.scroll_up(info.height)
            self.refresh_window()

    def scroll_info_down(self):
        self.information_win.scroll_down(self.information_win.height)
        if not isinstance(self.current_tab(), tabs.RosterInfoTab):
            self.information_win.refresh()
        else:
            info = self.current_tab().information_win
            info.scroll_down(info.height)
            self.refresh_window()

    def pop_information_win_up(self, size, time):
        """
        Temporarly increase the size of the information win of size lines
        during time seconds.
        After that delay, the size will decrease from size lines.
        """
        if time <= 0 or size <= 0:
            return
        result = self.grow_information_win(size)
        timed_event = timed_events.DelayedEvent(time, self.shrink_information_win, result)
        self.add_timed_event(timed_event)
        self.refresh_window()

    def toggle_left_pane(self):
        """
        Enable/disable the left panel.
        """
        enabled = config.get('enable_vertical_tab_list', False)
        if not config.silent_set('enable_vertical_tab_list', str(not enabled)):
            self.information(_('Unable to write in the config file'), 'Error')
        self.call_for_resize()

    def resize_global_information_win(self):
        """
        Resize the global_information_win only once at each resize.
        """
        with g_lock:
            self.information_win.resize(self.information_win_size, tabs.Tab.width,
                                        tabs.Tab.height - 1 - self.information_win_size - tabs.Tab.tab_win_height(), 0)

    def resize_global_info_bar(self):
        """
        Resize the GlobalInfoBar only once at each resize
        """
        with g_lock:
            self.tab_win.resize(1, tabs.Tab.width, tabs.Tab.height - 2, 0)
            if config.get('enable_vertical_tab_list', False):
                height, width = self.stdscr.getmaxyx()
                truncated_win = self.stdscr.subwin(height, config.get('vertical_tab_list_size', 20), 0, 0)
                self.left_tab_win = windows.VerticalGlobalInfoBar(truncated_win)
            else:
                self.left_tab_win = None

    def add_message_to_text_buffer(self, buff, txt, time=None, nickname=None, history=None):
        """
        Add the message to the room if possible, else, add it to the Info window
        (in the Info tab of the info window in the RosterTab)
        """
        if not buff:
            self.information('Trying to add a message in no room: %s' % txt, 'Error')
        else:
            buff.add_message(txt, time, nickname, history=history)

    def full_screen_redraw(self):
        """
        Completely erase and redraw the screen
        """
        self.stdscr.clear()
        self.refresh_window()

    def call_for_resize(self):
        """
        Called when we want to resize the screen
        """
        # If we have the tabs list on the left, we just give a truncated
        # window to each Tab class, so the draw themself in the portion
        # of the screen that the can occupy, and we draw the tab list
        # on the left remaining space
        if config.get('enable_vertical_tab_list', False):
            with g_lock:
                scr = self.stdscr.subwin(0, config.get('vertical_tab_list_size', 20))
        else:
            scr = self.stdscr
        tabs.Tab.resize(scr)
        self.resize_global_info_bar()
        self.resize_global_information_win()
        with g_lock:
            for tab in self.tabs:
                if config.get('lazy_resize', True):
                    tab.need_resize = True
                else:
                    tab.resize()
            if self.tabs:
                self.full_screen_redraw()

    def read_keyboard(self):
        """
        Get the next keyboard key pressed and returns it.
        get_user_input() has a timeout: it returns None when the timeout
        occurs. In that case we do not return (we loop until we get
        a non-None value), but we check for timed events instead.
        """
        res = keyboard.get_user_input(self.stdscr)
        while res is None:
            self.check_timed_events()
            res = keyboard.get_user_input(self.stdscr)
        return res

    def escape_next_key(self):
        """
        Tell the Keyboard object that the next key pressed by the user
        should be escaped. See Keyboard.get_user_input
        """
        keyboard.escape_next_key()

####################### Commands and completions ##############################

    def register_command(self, name, func, *, desc='', shortdesc='', completion=None, usage=''):
        if name in self.commands:
            return
        if not desc and shortdesc:
            desc = shortdesc
        self.commands[name] = Command(func, desc, completion, shortdesc, usage)
    def register_initial_commands(self):
        """
        Register the commands when poezio starts
        """
        self.register_command('help', self.command_help,
                usage=_('[command]'),
                shortdesc='\_o< KOIN KOIN KOIN',
                completion=self.completion_help)
        self.register_command('join', self.command_join,
                usage=_("[room_name][@server][/nick] [password]"),
                desc=_("Join the specified room. You can specify a nickname "
                    "after a slash (/). If no nickname is specified, you will"
                    " use the default_nick in the configuration file. You can"
                    " omit the room name: you will then join the room you\'re"
                    " looking at (useful if you were kicked). You can also "
                    "provide a room_name without specifying a server, the "
                    "server of the room you're currently in will be used. You"
                    " can also provide a password to join the room.\nExamples"
                    ":\n/join room@server.tld\n/join room@server.tld/John\n"
                    "/join room2\n/join /me_again\n/join\n/join room@server"
                    ".tld/my_nick password\n/join / password"),
                shortdesc=_('Join a room'),
                completion=self.completion_join)
        self.register_command('exit', self.command_quit,
                desc=_('Just disconnect from the server and exit poezio.'),
                shortdesc=_('Exit poezio.'))
        self.register_command('quit', self.command_quit,
                desc=_('Just disconnect from the server and exit poezio.'),
                shortdesc=_('Exit poezio.'))
        self.register_command('next', self.rotate_rooms_right,
                shortdesc=_('Go to the next room.'))
        self.register_command('prev', self.rotate_rooms_left,
                shortdesc=_('Go to the previous room.'))
        self.register_command('win', self.command_win,
                usage=_('<number or name>'),
                shortdesc=_('Go to the specified room'),
                completion=self.completion_win)
        self.commands['w'] = self.commands['win']
        self.register_command('move_tab', self.command_move_tab,
                usage=_('<source> <destination>'),
                desc=_("Insert the <source> tab at the position of "
                    "<destination>. This will make the following tabs shift in"
                    " some cases (refer to the documentation). A tab can be "
                    "designated by its number or by the beginning of its "
                    "address."),
                shortdesc=_('Move a tab.'),
                completion=self.completion_move_tab)
        self.register_command('show', self.command_status,
                usage=_('<availability> [status message]'),
                desc=_("Sets your availability and (optionally) your status "
                    "message. The <availability> argument is one of \"available"
                    ", chat, away, afk, dnd, busy, xa\" and the optional "
                    "[status message] argument will be your status message."),
                shortdesc=_('Change your availability.'),
                completion=self.completion_status)
        self.commands['status'] = self.commands['show']
        self.register_command('bookmark_local', self.command_bookmark_local,
                usage=_("[roomname][/nick] [password]"),
                desc=_("Bookmark Local: Bookmark locally the specified room "
                    "(you will then auto-join it on each poezio start). This"
                    " commands uses almost the same syntaxe as /join. Type "
                    "/help join for syntax examples. Note that when typing "
                    "\"/bookmark\" on its own, the room will be bookmarked "
                    "with the nickname you\'re currently using in this room "
                    "(instead of default_nick)"),
                shortdesc=_('Bookmark a room locally.'),
                completion=self.completion_bookmark_local)
        self.register_command('bookmark', self.command_bookmark,
                usage=_("[roomname][/nick] [autojoin] [password]"),
                desc=_("Bookmark: Bookmark online the specified room (you "
                    "will then auto-join it on each poezio start if autojoin"
                    " is specified and is 'true'). This commands uses almost"
                    " the same syntax as /join. Type /help join for syntax "
                    "examples. Note that when typing \"/bookmark\" alone, the"
                    " room will be bookmarked with the nickname you\'re "
                    "currently using in this room (instead of default_nick)."),
                shortdesc=_("Bookmark a room online."),
                completion=self.completion_bookmark)
        self.register_command('set', self.command_set,
                usage=_("[plugin|][section] <option> [value]"),
                desc=_("Set the value of an option in your configuration file."
                    " You can, for example, change your default nickname by "
                    "doing `/set default_nick toto` or your resource with `/set"
                    "resource blabla`. You can also set options in specific "
                    "sections with `/set bindings M-i ^i` or in specific plugin"
                    " with `/set mpd_client| host 127.0.0.1`. `toggle` can be "
                    "used as a special value to toggle a boolean option."),
                shortdesc=_("Set the value of an option"),
                completion=self.completion_set)
        self.register_command('theme', self.command_theme,
                usage=_('[theme name]'),
                desc=_("Reload the theme defined in the config file. If theme"
                    "_name is provided, set that theme before reloading it."),
                shortdesc=_('Load a theme'),
                completion=self.completion_theme)
        self.register_command('list', self.command_list,
                usage=_('[server]'),
                desc=_("Get the list of public chatrooms"
                    " on the specified server."),
                shortdesc=_('List the rooms.'),
                completion=self.completion_list)
        self.register_command('message', self.command_message,
                usage=_('<jid> [optional message]'),
                desc=_("Open a conversation with the specified JID (even if it"
                    " is not in our roster), and send a message to it, if the "
                    "message is specified."),
                shortdesc=_('Send a message'),
                completion=self.completion_message)
        self.register_command('version', self.command_version,
                usage='<jid>',
                desc=_("Get the software version of the given JID (usually its"
                    " XMPP client and Operating System)."),
                shortdesc=_('Get the software version of a JID.'),
                completion=self.completion_version)
        self.register_command('server_cycle', self.command_server_cycle,
                usage=_('[domain] [message]'),
                desc=_('Disconnect and reconnect in all the rooms in domain.'),
                shortdesc=_('Cycle a range of rooms'),
                completion=self.completion_server_cycle)
        self.register_command('bind', self.command_bind,
                usage=_(' <key> <equ>'),
                desc=_("Bind a key to another key or to a “command”. For "
                    "example \"/bind ^H KEY_UP\" makes Control + h do the"
                    " same same as the Up key."),
                completion=self.completion_bind,
                shortdesc=_('Bind a key to another key.'))
        self.register_command('load', self.command_load,
                usage=_('<plugin>'),
                shortdesc=_('Load the specified plugin'),
                completion=self.plugin_manager.completion_load)
        self.register_command('unload', self.command_unload,
                usage=_('<plugin>'),
                shortdesc=_('Unload the specified plugin'),
                completion=self.plugin_manager.completion_unload)
        self.register_command('plugins', self.command_plugins,
                shortdesc=_('Show the plugins in use.'))
        self.register_command('presence', self.command_presence,
                usage=_('<JID> [type] [status]'),
                desc=_("Send a directed presence to <JID> and using"
                    " [type] and [status] if provided."),
                shortdesc=_('Send a directed presence.'),
                completion=self.completion_presence)
        self.register_command('rawxml', self.command_rawxml,
                usage='<xml>',
                shortdesc=_('Send a custom xml stanza.'))
        self.register_command('invite', self.command_invite,
                usage=_('<jid> <room> [reason]'),
                desc=_('Invite jid in room with reason.'),
                shortdesc=_('Invite someone in a room.'),
                completion=self.completion_invite)
        self.register_command('invitations', self.command_invitations,
                shortdesc=_('Show the pending invitations.'))
        self.register_command('bookmarks', self.command_bookmarks,
                shortdesc=_('Show the current bookmarks.'))
        self.register_command('remove_bookmark', self.command_remove_bookmark,
                usage='[jid]',
                desc=_("Remove the specified bookmark, or the "
                    "bookmark on the current tab, if any."),
                shortdesc=_('Remove a bookmark'),
                completion=self.completion_remove_bookmark)
        self.register_command('xml_tab', self.command_xml_tab,
                shortdesc=_('Open an XML tab.'))
        self.register_command('runkey', self.command_runkey,
                usage=_('<key>'),
                shortdesc=_('Execute the action defined for <key>.'),
                completion=self.completion_runkey)
        self.register_command('self', self.command_self,
                shortdesc=_('Remind you of who you are.'))
        self.register_command('last_activity', self.command_last_activity,
                usage='<jid>',
                desc=_('Informs you of the last activity of a JID.'),
                shortdesc=_('Get the activity of someone.'),
                completion=self.completion_last_activity)

        if config.get('enable_user_activity', True):
            self.register_command('activity', self.command_activity,
                    usage='[<general> [specific] [text]]',
                    desc=_('Send your current activity to your contacts (use the completion).'
                           ' Nothing means "stop broadcasting an activity".'),
                    shortdesc=_('Send your activity.'),
                    completion=self.completion_activity)
        if config.get('enable_user_mood', True):
            self.register_command('mood', self.command_mood,
                    usage='[<mood> [text]]',
                    desc=_('Send your current mood to your contacts (use the completion).'
                           ' Nothing means "stop broadcasting a mood".'),
                    shortdesc=_('Send your mood.'),
                    completion=self.completion_mood)
        if config.get('enable_user_gaming', True):
            self.register_command('gaming', self.command_gaming,
                    usage='[<game name> [server address]]',
                    desc=_('Send your current gaming activity to your contacts.'
                           ' Nothing means "stop broadcasting a gaming activity".'),
                    shortdesc=_('Send your gaming activity.'),
                    completion=None)

####################### XMPP Event Handlers  ##################################
    on_session_start_features = handlers.on_session_start_features
    on_carbon_received = handlers.on_carbon_received
    on_carbon_sent = handlers.on_carbon_sent
    on_groupchat_invite = handlers.on_groupchat_invite
    on_groupchat_decline = handlers.on_groupchat_decline
    on_message = handlers.on_message
    on_normal_message = handlers.on_normal_message
    on_nick_received = handlers.on_nick_received
    on_gaming_event = handlers.on_gaming_event
    on_mood_event = handlers.on_mood_event
    on_activity_event = handlers.on_activity_event
    on_tune_event = handlers.on_tune_event
    on_groupchat_message = handlers.on_groupchat_message
    on_muc_own_nickchange = handlers.on_muc_own_nickchange
    on_groupchat_private_message = handlers.on_groupchat_private_message
    on_chatstate_active = handlers.on_chatstate_active
    on_chatstate_inactive = handlers.on_chatstate_inactive
    on_chatstate_composing = handlers.on_chatstate_composing
    on_chatstate_paused = handlers.on_chatstate_paused
    on_chatstate_gone = handlers.on_chatstate_gone
    on_chatstate = handlers.on_chatstate
    on_chatstate_normal_conversation = handlers.on_chatstate_normal_conversation
    on_chatstate_private_conversation = handlers.on_chatstate_private_conversation
    on_chatstate_groupchat_conversation = handlers.on_chatstate_groupchat_conversation
    on_roster_update = handlers.on_roster_update
    on_subscription_request = handlers.on_subscription_request
    on_subscription_authorized = handlers.on_subscription_authorized
    on_subscription_remove = handlers.on_subscription_remove
    on_subscription_removed = handlers.on_subscription_removed
    on_presence = handlers.on_presence
    on_presence_error = handlers.on_presence_error
    on_got_offline = handlers.on_got_offline
    on_got_online = handlers.on_got_online
    on_groupchat_presence = handlers.on_groupchat_presence
    on_failed_connection = handlers.on_failed_connection
    on_disconnected = handlers.on_disconnected
    on_failed_auth = handlers.on_failed_auth
    on_no_auth = handlers.on_no_auth
    on_connected = handlers.on_connected
    on_session_start = handlers.on_session_start
    on_status_codes = handlers.on_status_codes
    on_groupchat_subject = handlers.on_groupchat_subject
    on_data_form = handlers.on_data_form
    on_attention = handlers.on_attention
    room_error = handlers.room_error
    outgoing_stanza = handlers.outgoing_stanza
    incoming_stanza = handlers.incoming_stanza
    validate_ssl = handlers.validate_ssl
    command_help = commands.command_help
    command_runkey = commands.command_runkey
    command_status = commands.command_status
    command_presence = commands.command_presence
    command_theme = commands.command_theme
    command_win = commands.command_win
    command_move_tab = commands.command_move_tab
    command_list = commands.command_list
    command_version = commands.command_version
    command_join = commands.command_join
    command_bookmark_local = commands.command_bookmark_local
    command_bookmark = commands.command_bookmark
    command_bookmarks = commands.command_bookmarks
    command_remove_bookmark = commands.command_remove_bookmark
    command_set = commands.command_set
    command_server_cycle = commands.command_server_cycle
    command_last_activity = commands.command_last_activity
    command_mood = commands.command_mood
    command_activity = commands.command_activity
    command_gaming = commands.command_gaming
    command_invite = commands.command_invite
    command_decline = commands.command_decline
    command_invitations = commands.command_invitations
    command_quit = commands.command_quit
    command_bind = commands.command_bind
    command_pubsub = commands.command_pubsub
    command_rawxml = commands.command_rawxml
    command_load = commands.command_load
    command_unload = commands.command_unload
    command_plugins = commands.command_plugins
    command_message = commands.command_message
    command_xml_tab = commands.command_xml_tab
    command_self = commands.command_self
    completion_help = completions.completion_help
    completion_status = completions.completion_status
    completion_presence = completions.completion_presence
    completion_theme = completions.completion_theme
    completion_win = completions.completion_win
    completion_join = completions.completion_join
    completion_version = completions.completion_version
    completion_list = completions.completion_list
    completion_move_tab = completions.completion_move_tab
    completion_runkey = completions.completion_runkey
    completion_bookmark = completions.completion_bookmark
    completion_remove_bookmark = completions.completion_remove_bookmark
    completion_decline = completions.completion_decline
    completion_bind = completions.completion_bind
    completion_message = completions.completion_message
    completion_invite = completions.completion_invite
    completion_activity = completions.completion_activity
    completion_mood = completions.completion_mood
    completion_last_activity = completions.completion_last_activity
    completion_server_cycle = completions.completion_server_cycle
    completion_set = completions.completion_set
    completion_bookmark_local = completions.completion_bookmark_local



class KeyDict(dict):
    """
    A dict, with a wrapper for get() that will return a custom value
    if the key starts with _exc_
    """
    def get(self, k, d=None):
        if isinstance(k, str) and k.startswith('_exc_') and len(k) > 5:
            return lambda: dict.get(self, '_exc_')(k[5:])
        return dict.get(self, k, d)

def replace_key_with_bound(key):
    bind = config.get(key, key, 'bindings')
    if not bind:
        bind = key
    return bind


