#!/usr/bin/env python

import os
import re
import shlex
import subprocess
import threading
import time
import webbrowser
import ConfigParser
from gi.repository import GLib, GdkPixbuf, Gtk
# Import special features if we're running Ubuntu Unity
if (os.environ.get('DESKTOP_SESSION') == 'ubuntu' or
        os.environ.get('DESKTOP_SESSION') == 'ubuntu-2d'):
    from gi.repository import Unity, Dbusmenu
    unity = True
else:
    unity = False


class RocketDepot:
    def __init__(self):
        # Default options.  Overridden by config file.
        self.options = {
            'host': 'host.example.com',
            # set the default RDP user to the local user by default
            'user': os.environ.get('USER', 'user'),
            'geometry': '1024x768',
            'program': 'xfreerdp',
            'homeshare': 'false',
            'grabkeyboard': 'false',
            'fullscreen': 'false',
            'clioptions': '',
            'terminal': 'false'
        }
        # Local user homedir and config file
        self.homedir = os.environ['HOME']
        self.create_config_dir()
        # Our config dotfile
        self.configfile = '%s/.config/rocket-depot/config.ini' % self.homedir
        self.config = ConfigParser.RawConfigParser()
        self.config.read(self.configfile)
        self.read_config('defaults')
        self.save_config('defaults')
        self.mw = MainWindow(self)

    # Create config dir
    def create_config_dir(self):
        configdir = '%s/.config/rocket-depot' % self.homedir
        if not os.path.exists(configdir):
            try:
                os.mkdir(configdir, 0700)
            except OSError:
                print 'Error:  Unable to create config directory.'

    # Open the config file for writing
    def write_config(self):
        with open(self.configfile, 'wb') as f:
            self.config.write(f)

    # Save options to the config file
    def save_config(self, section):
        # add the new section if it doesn't exist
        if not self.config.has_section(section):
            self.config.add_section(section)
        # Set all selected options
        for opt in self.options:
            self.config.set(section, opt, self.options[opt])
        self.write_config()

    # Delete a section from the config file
    def delete_config(self, section):
        self.config.remove_section(section)
        self.write_config()

    # Set options based on section in config file
    def read_config(self, section):
        if os.path.exists(self.configfile):
            for opt in self.options:
                if not self.config.has_option(section, opt):
                    self.options[opt] = ''
                else:
                    self.options[opt] = self.config.get(section, opt)

    # Make a list of all profiles in config file.  Sort the order
    # alphabetically, except special 'defaults' profile always comes first
    def list_profiles(self):
        profiles_list = sorted(self.config.sections())
        defaults_index = profiles_list.index('defaults')
        profiles_list.insert(0, profiles_list.pop(defaults_index))
        return profiles_list

    # Check for given host in freerdp's known_hosts file before connecting
    def check_known_hosts(self, host):
        known_hosts = '%s/.config/freerdp/known_hosts' % self.homedir
        try:
            with open(known_hosts, 'r') as f:
                read_data = f.read()
            match = re.search(host, read_data)
            if match:
                return True
            else:
                return False
        except IOError:
            return False

    # Run the selected RDP client - currently rdesktop or xfreerdp
    def run_program(self):
        # CLI parameters for each RDP client we support.  stdopts are always
        # used.
        client_opts = {
            'rdesktop': {
                'stdopts': ['rdesktop', '-a16'],
                'host': '',
                'user': '-u',
                'geometry': '-g',
                'homeshare': '-rdisk:home=' + self.homedir,
                'grabkeyboard': '-K',
                'fullscreen': '-f'
            },
            'xfreerdp': {
                'stdopts': ['xfreerdp', '+clipboard'],
                'host': '/v:',
                'user': '/u:',
                'geometry': '/size:',
                'homeshare': '/drive:home,' + self.homedir,
                'grabkeyboard': '-grab-keyboard',
                'fullscreen': '/f'
            }
        }

        # This makes the next bit a little cleaner name-wise
        client = self.options['program']
        # List of commandline paramenters for our RDP client
        params = []
        # Add standard options to the parameter list
        for x in client_opts[client]['stdopts']:
            params.append(x)
        # Add specified options to the parameter list
        if self.options['user'] != '':
            # We put quotes around the username so that the domain\username
            # format doesn't get escaped
            slashuser = "'%s'" % str.strip(self.options['user'])
            params.append(client_opts[client]['user'] + slashuser)
        # Detect percent symbol in geometry field.  If it exists we do math to
        # use the correct resolution for the active monitor.  Otherwise we
        # submit a given resolution such as 1024x768 to the list of parameters.
        if self.options['geometry'] != '':
            geo = client_opts[client]['geometry']
            if self.options['geometry'].find('%') == -1:
                params.append(geo
                              + '%s' % str.strip(self.options['geometry']))
            else:
                params.append(geo
                              + self.mw.geo_percent(self.options['geometry']))
        if self.options['fullscreen'] == 'true':
            params.append(client_opts[client]['fullscreen'])
        if self.options['grabkeyboard'] == 'false':
            params.append(client_opts[client]['grabkeyboard'])
        if self.options['homeshare'] == 'true':
            params.append(client_opts[client]['homeshare'])
        if self.options['clioptions'] != '':
            params.append(self.options['clioptions'])
        # Hostname goes last in the list of parameters
        params.append(client_opts[client]['host']
                      + '%s' % str.strip(self.options['host']))
        # Clean up params list to make it shell compliant
        cmdline = shlex.split(' '.join(params))
        self.terminal_needed(self.options['host'], cmdline)
        return cmdline

    # Open a terminal when freerdp needs user input
    def terminal_needed(self, host, cmdline):
        terminal_args = ['xterm', '-hold', '-e']

        def prepend_terminal():
            if cmdline[0] != terminal_args[0]:
                for x in reversed(terminal_args):
                    cmdline.insert(0, x)
        if cmdline[0] == 'xfreerdp':
            if '-sec-nla' not in cmdline:
                prepend_terminal()
            if '/cert-ignore' not in cmdline and self.check_known_hosts(host) is False:
                prepend_terminal()
        if self.options['terminal'] == 'true':
            prepend_terminal()


# Thread for RDP client launch feedback in UI
class WorkerThread(threading.Thread):
    def __init__(self, callback, cmdline):
        threading.Thread.__init__(self)
        self.callback = callback
        self.cmdline = cmdline
        WorkerThread.error_text = ''
        WorkerThread.return_code = 0

    # Start the client and wait some seconds for errors
    def run(self):
        # Print the command line that we constructed to the terminal
        print 'Command to execute: \n' + ' '.join(str(x) for x in self.cmdline)
        p = subprocess.Popen(self.cmdline, stderr=subprocess.PIPE)
        start_time = time.time()
        while p.poll() is None:
            time.sleep(1)
            if time.time() - start_time > 3:
                break
        if p.poll() is not None:
            WorkerThread.error_text += p.communicate()[1]
            WorkerThread.return_code += p.returncode
        GLib.idle_add(self.callback)


# GUI stuff
class MainWindow(Gtk.Window):
    def __init__(self, rd):
        # Window properties
        self.rd = rd
        Gtk.Window.__init__(self, title="Rocket Depot", resizable=0)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(8)
        self.set_wmclass('rocket-depot', 'rocket-depot')

        # Menu bar layout
        self.UI_INFO = """
        <ui>
          <menubar name='MenuBar'>
            <menu action='FileMenu'>
              <menuitem action='SaveCurrentConfig' />
              <menuitem action='SaveCurrentConfigAsDefault' />
              <menuitem action='DeleteCurrentConfig' />
              <menuitem action='FileQuit' />
            </menu>
            <menu action='Help'>
              <menuitem action='FreeRDPDocs'/>
              <menuitem action='rdesktopDocs'/>
              <menuitem action='About'/>
            </menu>
          </menubar>
        </ui>
        """

        # Menu bar
        action_group = Gtk.ActionGroup(name="Menu")
        self.add_file_menu_actions(action_group)
        self.add_help_menu_actions(action_group)
        uimanager = self.create_ui_manager()
        uimanager.insert_action_group(action_group)
        menubar = uimanager.get_widget("/MenuBar")

        # Grid for widgets in main window
        grid = Gtk.Grid()
        grid.set_row_spacing(4)
        self.add(grid)

        # Labels for text entry fields and comboboxes
        profileslabel = Gtk.Label(label="Profile")
        hostlabel = Gtk.Label(label="Host")
        userlabel = Gtk.Label(label="Username")
        geometrylabel = Gtk.Label(label="Geometry")
        clioptionslabel = Gtk.Label(label="CLI Options")
        programlabel = Gtk.Label(label="RDP Client")

        # Profiles combobox
        self.profiles_combo = Gtk.ComboBoxText.new_with_entry()
        self.profiles_combo.set_tooltip_text('List of saved connection '
                                             'profiles')
        self.populate_profiles_combobox()
        self.profiles_combo.connect("changed", self.on_profiles_combo_changed)
        # If an existing profile name has been typed into the profiles
        # combobox, allow the 'enter' key to launch the RDP client
        profiles_combo_entry = self.profiles_combo.get_children()[0]
        profiles_combo_entry.connect("activate", self.enter_connect,
                                     profiles_combo_entry)

        # Text entry fields
        self.hostentry = Gtk.Entry()
        self.hostentry.set_tooltip_text('Hostname or IP address of RDP server')
        self.hostentry.connect("activate", self.enter_connect, self.hostentry)
        self.userentry = Gtk.Entry()
        self.userentry.set_tooltip_text('''RDP username.
Domain credentials may be entered in domain\username format:
e.g. "example.com\myusername"''')
        self.userentry.connect("activate", self.enter_connect, self.userentry)
        self.geometryentry = Gtk.Entry()
        self.geometryentry.set_tooltip_text('''Resolution of RDP window.
Can be set to a specific resolution or a percentage:
e.g. "1024x768" or "80%"''')
        self.geometryentry.connect("activate",
                                   self.enter_connect, self.geometryentry)
        self.clioptionsentry = Gtk.Entry()
        self.clioptionsentry.set_tooltip_text('''Extra CLI options''')
        self.clioptionsentry.connect("activate",
                                     self.enter_connect, self.clioptionsentry)

        # Radio button for program selection
        self.xfreerdpbutton = Gtk.RadioButton.new_with_label_from_widget(None, "FreeRDP")
        self.xfreerdpbutton.connect("toggled", self.on_radio_button_toggled,
                                    "xfreerdp")
        self.rdesktopbutton = Gtk.RadioButton.new_from_widget(self.xfreerdpbutton)
        self.rdesktopbutton.set_label("rdesktop")
        self.rdesktopbutton.connect("toggled", self.on_radio_button_toggled,
                                    "rdesktop")
        self.xfreerdpbutton.set_tooltip_text('Choose a supported RDP client')
        self.rdesktopbutton.set_tooltip_text('Choose a supported RDP client')

        # Checkbox for sharing our home directory
        self.homedirbutton = Gtk.CheckButton(label="Share Home Dir")
        self.homedirbutton.set_tooltip_text('Share local home directory with '
                                            'RDP server')
        self.homedirbutton.connect("toggled", self.on_button_toggled,
                                   "homeshare")

        # Checkbox for grabbing the keyboard
        self.grabkeyboardbutton = Gtk.CheckButton(label="Grab Keyboard")
        self.grabkeyboardbutton.set_tooltip_text('Send all keyboard inputs to '
                                                 'RDP server')
        self.grabkeyboardbutton.connect("toggled", self.on_button_toggled,
                                        "grabkeyboard")

        # Checkbox for fullscreen view
        self.fullscreenbutton = Gtk.CheckButton(label="Fullscreen")
        self.fullscreenbutton.set_tooltip_text('Run RDP client in fullscreen '
                                               'mode')
        self.fullscreenbutton.connect("toggled", self.on_button_toggled,
                                      "fullscreen")

        # Checkbox for terminal
        self.terminalbutton = Gtk.CheckButton(label="Terminal")
        self.terminalbutton.set_tooltip_text('''Run RDP client from terminal.
Useful for diagnosing connection problems''')
        self.terminalbutton.connect("toggled", self.on_button_toggled,
                                    "terminal")

        # Progress spinner
        self.spinner = Gtk.Spinner()

        # Connect button
        self.connectbutton = Gtk.Button(label="Connect")
        self.connectbutton.connect("clicked", self.enter_connect)

        # Grid to which we attach all of our widgets
        grid.attach(menubar, 0, 0, 12, 4)
        grid.attach(profileslabel, 0, 4, 4, 4)
        grid.attach(hostlabel, 0, 8, 4, 4)
        grid.attach(userlabel, 0, 12, 4, 4)
        grid.attach(geometrylabel, 0, 16, 4, 4)
        grid.attach(clioptionslabel, 0, 20, 4, 4)
        grid.attach(programlabel, 0, 24, 4, 4)
        grid.attach(self.homedirbutton, 0, 28, 4, 4)
        grid.attach(self.terminalbutton, 0, 32, 4, 4)
        grid.attach_next_to(self.profiles_combo, profileslabel,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.hostentry, hostlabel,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.userentry, userlabel,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.geometryentry, geometrylabel,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.clioptionsentry, clioptionslabel,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.xfreerdpbutton, programlabel,
                            Gtk.PositionType.RIGHT, 4, 4)
        grid.attach_next_to(self.rdesktopbutton, self.xfreerdpbutton,
                            Gtk.PositionType.RIGHT, 4, 4)
        grid.attach_next_to(self.grabkeyboardbutton, self.homedirbutton,
                            Gtk.PositionType.RIGHT, 4, 4)
        grid.attach_next_to(self.fullscreenbutton, self.grabkeyboardbutton,
                            Gtk.PositionType.RIGHT, 4, 4)
        grid.attach_next_to(self.connectbutton, self.terminalbutton,
                            Gtk.PositionType.RIGHT, 8, 4)
        grid.attach_next_to(self.spinner, self.terminalbutton,
                            Gtk.PositionType.RIGHT, 8, 4)

        # Load the default profile on startup
        self.load_settings()
        self.profilename = 'defaults'
        # Set up Unity quicklist if we can support that
        if unity is True:
            self.create_unity_quicklist()

    # If a geometry percentage is given, let's figure out the actual resolution
    def geo_percent(self, geometry):
        # Remove the percent symbol from our value
        cleangeo = int(re.sub('[^0-9]', '', geometry))
        # Get the screen from the GtkWindow
        screen = self.get_screen()
        # Using the screen of the Window, the monitor it's on can be identified
        monitor = screen.get_monitor_at_window(screen.get_active_window())
        # Then get the geometry of that monitor
        mongeometry = screen.get_monitor_geometry(monitor)
        # Move our geometry percent decimal place two to the left so that we
        # can multiply
        cleangeo /= 100.
        # Multiply current width and height to find requested width and height
        width = int(round(cleangeo * mongeometry.width))
        height = int(round(cleangeo * mongeometry.height))
        return "%sx%s" % (width, height)

    # Each section in the config file gets an entry in the profiles combobox
    def populate_profiles_combobox(self):
        self.profiles_combo.get_model().clear()
        for profile in self.rd.list_profiles():
            if profile != 'defaults':
                self.profiles_combo.append_text(profile)

    # Each section in the config file gets an entry in the Unity quicklist
    def populate_unity_quicklist(self):
        for profile in self.rd.list_profiles():
            self.update_unity_quicklist(profile)

    # Create the Unity quicklist and populate it with our profiles
    def create_unity_quicklist(self):
        entry = Unity.LauncherEntry.get_for_desktop_id("rocket-depot.desktop")
        self.quicklist = Dbusmenu.Menuitem.new()
        self.populate_unity_quicklist()
        entry.set_property("quicklist", self.quicklist)

    # Append a new profile to the Unity quicklist
    def update_unity_quicklist(self, profile):
        if profile != 'defaults':
            profile_menu_item = Dbusmenu.Menuitem.new()
            profile_menu_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL,
                                           profile)
            profile_menu_item.property_set_bool(Dbusmenu.MENUITEM_PROP_VISIBLE,
                                                True)
            profile_menu_item.connect("item-activated", self.on_unity_clicked,
                                      profile)
            self.quicklist.child_append(profile_menu_item)

    # If we delete a profile we must delete all Unity quicklist entries and
    # rebuild the quicklist
    def clean_unity_quicklist(self):
        for x in self.quicklist.get_children():
            self.quicklist.child_delete(x)
        self.populate_unity_quicklist()

    def start_thread(self):
        # Throw an error if the required host field is empty
        if not self.rd.options['host']:
            self.on_warn(None, 'No Host', 'No Host or IP Address Given')
        else:
            self.connectbutton.hide()
            self.spinner.show()
            self.spinner.start()
            cmdline = self.rd.run_program()
            thread = WorkerThread(self.work_finished_cb, cmdline)
            thread.start()

    # Triggered when a profile is selected via the Unity quicklist
    def on_unity_clicked(self, widget, entry, profile):
        self.rd.read_config(profile)
        self.start_thread()

    # Trigged when we press 'Enter' or the 'Connect' button
    def enter_connect(self, *args):
        self.grab_textboxes()
        self.start_thread()

    def work_finished_cb(self):
        self.spinner.stop()
        self.spinner.hide()
        self.connectbutton.show()
        error_text = WorkerThread.error_text
        return_code = WorkerThread.return_code
        # return code 62 is a benign error code from rdesktop
        # return code 62 is not used by xfreerdp
        if return_code is not 0 and return_code is not 62:
            # discard extra data from long error messages
            if len(error_text) > 300:
                error_text = error_text[:300] + '...'
            self.on_warn(None, 'Connection Error', '%s: \n'
                         % self.rd.options['program'] + error_text)

    # Triggered when the combobox is clicked.  We load the selected profile
    # from the config file.
    def on_profiles_combo_changed(self, combo):
        text = combo.get_active_text()
        # Should we really iterate over the list of profiles here?
        for profile in self.rd.list_profiles():
            if text == profile:
                self.rd.read_config(text)
                self.load_settings()
        self.profilename = text

    # Triggered when the checkboxes are toggled
    def on_button_toggled(self, button, name):
        if button.get_active():
            state = 'true'
            self.rd.options[name] = state
        else:
            state = 'false'
            self.rd.options[name] = state

    # Triggered when the program radio buttons are toggled
    def on_radio_button_toggled(self, button, name):
        if button.get_active():
            state = 'true'
            self.rd.options['program'] = name

    # Triggered when the file menu is used
    def add_file_menu_actions(self, action_group):
        action_filemenu = Gtk.Action(name="FileMenu", label="File",
                                     tooltip=None, stock_id=None)
        action_group.add_action(action_filemenu)
        # Why do the functions here execute on startup if we add parameters?
        action_group.add_actions([("SaveCurrentConfig", None,
                                   "Save Current Profile", "<control>S", None,
                                   self.save_current_config)])
        action_group.add_actions([("SaveCurrentConfigAsDefault", None,
                                   "Save Current Profile as Default", "<control>D",
                                   None, self.save_current_config_as_default)])
        action_group.add_actions([("DeleteCurrentConfig", None,
                                   "Delete Current Profile", "<control><shift>S", None,
                                   self.delete_current_config)])
        action_group.add_actions([("FileQuit", None,
                                   "Quit", "<control>Q", None,
                                   self.quit)])

    # Triggered when the help menu is used
    def add_help_menu_actions(self, action_group):
        action_group.add_actions([
            ("Help", None, "Help"),
            ("About", None, "About", None, None, self.on_menu_help_about),
            ("FreeRDPDocs", None, "FreeRDP Web Documentation", None, None, self.on_menu_xfreerdp_help),
            ("rdesktopDocs", None, "rdesktop Web Documentation", None, None, self.on_menu_rdesktop_help),
        ])

    # Needed for the menu bar
    def create_ui_manager(self):
        uimanager = Gtk.UIManager()
        # Throws exception if something went wrong
        uimanager.add_ui_from_string(self.UI_INFO)
        # Add the accelerator group to the toplevel window
        accelgroup = uimanager.get_accel_group()
        self.add_accel_group(accelgroup)
        return uimanager

    # When the save config button is clicked on the menu bar
    def save_current_config(self, widget):
        if self.profilename == '' or self.profilename == 'defaults':
            self.on_warn(None, 'No Profile Name',
                         'Please name your profile before saving.')
        else:
            self.grab_textboxes()
            self.rd.save_config(self.profilename)
            self.populate_profiles_combobox()
            if unity is True:
                self.clean_unity_quicklist()

    # When the delete config button is clicked on the menu bar
    def delete_current_config(self, widget):
        if self.profilename == '' or self.profilename == 'defaults':
            self.on_warn(None, 'Select a Profile',
                         'Please select a profile to delete.')
        else:
            self.rd.delete_config(self.profilename)
            # reload the default config
            self.rd.read_config('defaults')
            self.load_settings()
            # Set profiles combobox to have no active item
            self.profiles_combo.set_active(-1)
            # Add a blank string to the head end of the combobox to 'clear' it
            self.profiles_combo.prepend_text('')
            # Set the blank string active to again, to 'clear' the combobox
            self.profiles_combo.set_active(0)
            # Now that we've 'cleared' the combobox text, let's delete the
            # blank entry and then repopulate the entire combobox
            active = self.profiles_combo.get_active()
            self.profiles_combo.remove(active)
            self.populate_profiles_combobox()
            if unity is True:
                self.clean_unity_quicklist()

    # When the save config button is clicked on the menu bar
    def save_current_config_as_default(self, widget):
        self.grab_textboxes()
        self.rd.save_config('defaults')

    # When the quit button is clicked on the menu bar
    def quit(self, widget):
        Gtk.main_quit()

    # When the help button is clicked on the menu bar
    def on_menu_help_about(self, widget):
        self.on_about(widget)

    # When the FreeRDP help button is clicked on the menu bar
    def on_menu_xfreerdp_help(self, widget):
        url = "https://github.com/FreeRDP/FreeRDP/wiki/CommandLineInterface"
        webbrowser.open_new_tab(url)

    # When the rdesktop help button is clicked on the menu bar
    def on_menu_rdesktop_help(self, widget):
        url = "http://linux.die.net/man/1/rdesktop"
        webbrowser.open_new_tab(url)

    # Grab all textbox input
    def grab_textboxes(self):
        self.rd.options['host'] = self.hostentry.get_text()
        self.rd.options['user'] = self.userentry.get_text()
        self.rd.options['geometry'] = self.geometryentry.get_text()
        self.rd.options['clioptions'] = self.clioptionsentry.get_text()

    # Generic warning dialog
    def on_warn(self, widget, title, message):
        dialog = Gtk.MessageDialog(transient_for=self, flags=0,
                                   message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK, text=title,
                                   title='Rocket Depot')
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    # About dialog
    def on_about(self, widget):
        about = Gtk.AboutDialog()
        about.set_program_name("Rocket Depot")
        about.set_version("0.22")
        about.set_copyright("2014 David Roble")
        about.set_comments("rdesktop/xfreerdp Frontend")
        about.set_website("https://github.com/robled/rocket-depot")
        about.set_logo(GdkPixbuf.Pixbuf.new_from_file("/usr/share/icons/hicolor/scalable/apps/rocket-depot.svg"))
        about.run()
        about.destroy()

    # Load all settings
    def load_settings(self):
        self.hostentry.set_text(self.rd.options['host'])
        self.userentry.set_text(self.rd.options['user'])
        self.geometryentry.set_text(self.rd.options['geometry'])
        self.clioptionsentry.set_text(self.rd.options['clioptions'])
        if self.rd.options['program'] == 'xfreerdp':
            self.xfreerdpbutton.set_active(True)
        if self.rd.options['program'] == 'rdesktop':
            self.rdesktopbutton.set_active(True)
        if self.rd.options['homeshare'] == 'true':
            self.homedirbutton.set_active(True)
        else:
            self.homedirbutton.set_active(False)
        if self.rd.options['grabkeyboard'] == 'true':
            self.grabkeyboardbutton.set_active(True)
        else:
            self.grabkeyboardbutton.set_active(False)
        if self.rd.options['fullscreen'] == 'true':
            self.fullscreenbutton.set_active(True)
        else:
            self.fullscreenbutton.set_active(False)
        if self.rd.options['terminal'] == 'true':
            self.terminalbutton.set_active(True)
        else:
            self.terminalbutton.set_active(False)


def _main():
    # Read the default profile and then save it if it doesn't already exist
    rocket_depot = RocketDepot()
    window = MainWindow(rocket_depot)
    window.connect("delete-event", Gtk.main_quit)
    window.show_all()
    # Hide the progress spinner until it is needed
    window.spinner.hide()
    # Set focus to the host entry box on startup
    window.hostentry.grab_focus()
    Gtk.main()


if __name__ == '__main__':
    _main()
