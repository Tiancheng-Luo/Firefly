import asyncio
import configparser
import importlib
import json
import signal
import sys
from os import path
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from pathlib import Path

from aiohttp import web

from Firefly import aliases, logging, scheduler
from Firefly.const import COMPONENT_MAP, DEVICE_FILE, EVENT_TYPE_BROADCAST, LOCATION_FILE, SERVICE_CONFIG_FILE, TIME, TYPE_DEVICE, VERSION, REQUIRED_FILES
from Firefly.helpers.events import (Event, Request)
from Firefly.helpers.groups.groups import import_groups
from Firefly.helpers.location import Location
from Firefly.helpers.room import Rooms
from Firefly.helpers.subscribers import Subscriptions

app = web.Application()


def sigterm_handler(_signo, _stack_frame):
  # Raises SystemExit(0):
  logging.notify('Firefly is shutting down...')
  sys.exit(0)


class Firefly(object):
  ''' Core running loop and scheduler of Firefly'''

  def __init__(self, settings):
    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGHUP, sigterm_handler)
    signal.signal(signal.SIGQUIT, sigterm_handler)

    # TODO: Most of this should be in startup not init.
    logging.Startup(self)
    logging.message('Initializing Firefly')

    self.check_required_files()

    # TODO (zpriddy): Add import and export of current state.
    self.current_state = {}

    self._firebase_enabled = False
    self._rooms = None
    self._components = {}
    self.settings = settings
    self.loop = asyncio.get_event_loop()

    self.executor = ThreadPoolExecutor(max_workers=10)
    self.loop.set_default_executor(self.executor)

    self._subscriptions = Subscriptions()

    self.location = self.import_location()

    # Get the beacon ID.
    self.beacon_id = settings.beacon_id

    # Start Notification service
    self.install_package('Firefly.services.notification', alias='service notification')

    # self.install_package('Firefly.components.notification.pushover', alias='Pushover', api_key='KEY', user_key='KEY')


    for c in COMPONENT_MAP:
      self.import_components(c['file'])

    self.install_services()

    # TODO: Rooms will be replaced by groups subclass rooms.
    self._rooms = Rooms(self)
    self._rooms.build_rooms()

    # Import Groups
    import_groups(self)


    logging.error(code='FF.COR.INI.001')  # this is a test error message
    logging.notify('Firefly is starting up in mode: %s' % self.location.mode)

    # TODO: Leave In.
    scheduler.runEveryH(1, self.export_all_components)

    # Set the current state for all devices.
    # TODO (zpriddy): Remove this when import and export is done.
    all_devices = set([c_id for c_id, c in self.components.items() if (c.type == TYPE_DEVICE or c.type == 'ROOM')])
    self.current_state = self.get_device_states(all_devices)

  def install_component(self, component):
    ''' Install a component into the core components.

    Args:
      component: Component object

    Returns:

    '''
    try:
      self.components[component.id] = component
      return component.id
    except Exception as e:
      logging.error('[CORE INSTALL COMPONENT] ERROR INSTALLING: %s' % str(e))
      return None


  def import_location(self) -> Location:
    ''' Import location data.

    Returns:

    '''
    return Location(self, LOCATION_FILE)

  def export_location(self) -> None:
    ''' Export location data.

    Returns:

    '''
    self.location.export_to_file()

  def check_required_files(self, **kwargs):
    ''' Make sure all required files are there. If not set to default content.

    Args:
      **kwargs:

    Returns:

    '''
    logging.info('[CORE] checking required files')
    for file_path, default_content in REQUIRED_FILES.items():
      if not path.isfile(file_path):
        if default_content is None:
          Path(file_path).touch()
        elif type(default_content) is dict or type(default_content) is list:
          with open(file_path, 'w') as new_file:
            json.dump(default_content, new_file)



  def install_services(self) -> None:
    config = configparser.ConfigParser()
    config.read(SERVICE_CONFIG_FILE)
    services = config.sections()

    for service in services:
      package = config.get(service, 'package')
      alias = ('service_%s' % service).lower()
      enabled = config.getboolean(service, 'enable', fallback=False)
      if not enabled:
        continue

      try:
        self.install_package(package, alias=alias)
      except Exception as e:
        logging.error(code='FF.COR.INS.001', args=(service, e))  # error installing package %s: %s
        logging.notify('Error installing package %s: %s' % (service, e))

    if self.components.get('service_firebase'):
      self._firebase_enabled = True

  def start(self) -> None:
    """
    Start up Firefly.
    """
    try:
      web.run_app(app, host=self.settings.firefly_host, port=self.settings.firefly_port)
    except KeyboardInterrupt:
      logging.message('Firefly was manually killed')
    except SystemExit:
      logging.message('Firefly was killed by system process. Probably due to automatic updates')
    finally:
      self.stop()

  def stop(self) -> None:
    ''' Shutdown firefly.

    Shutdown process should export the current state of all components so it can be imported on reboot and startup.
    '''
    logging.message('Stopping Firefly')

    self.export_all_components()
    self.export_location()

    try:
      logging.message('Stopping zwave service')
      if self.components.get('service_zwave'):
        self.components['service_zwave'].stop()
    except Exception as e:
      logging.notify(e)

    self.loop.stop()
    self.loop.close()

  @asyncio.coroutine
  def add_task(self, task):
    logging.debug('Adding task to Firefly scheduler: %s' % str(task))
    future = asyncio.Future()
    r = yield from asyncio.ensure_future(task)
    future.set_result(r)
    return r

  def delete_device(self, ff_id):
    self.components.pop(ff_id)
    aliases.aliases.pop(ff_id)
    if self.components.get('service_firebase'):
      self.components['service_firebase'].refresh_all()

  def export_all_components(self) -> None:
    """
    Export current values to backup files to restore current config on reboot.
    """
    logging.message('Exporting current config.')
    for c in COMPONENT_MAP:
      self.export_components(c['file'], c['type'])
    aliases.export_aliases()


  def import_components(self, config_file=DEVICE_FILE):
    ''' Import all components from the devices file

    Args:
      config_file: json file of all components

    Returns:

    '''
    logging.message('Importing components from config file: %s' % config_file)
    try:
      with open(config_file) as file:
        components = json.loads(file.read())
      for component in components:
        self.install_package(component.get('package'), **component)
    except Exception as e:
      logging.error('Error importing data from: %s - %s' % (config_file, str(e)))

  def export_components(self, config_file: str, component_type: str, current_values: bool = True) -> None:
    """
    Export all components with config and optional current states to a config file.

    Args:
      config_file (str): Path to config file.
      current_values (bool): Include current values.
    """
    logging.message('Exporting component and states to config file. - %s' % component_type)
    components = []
    for _, device in self.components.items():
      if device.type == component_type:
        components.append(device.export(current_values=current_values))

    with open(config_file, 'w') as file:
      json.dump(components, file, indent=4, sort_keys=True)

  def install_package(self, module: str, **kwargs):
    """
    Installs a package from the module. The package must support the Setup(firefly, **kwargs) function.

    The setup function can (and should) add the ff_id (if a ff_id) to the firefly._devices dict.

    Args:
      module (str): path to module being imported
      **kwargs (): If possible supply alias and/or device_id
    """
    logging.message('Installing module from %s %s' % (module, str(kwargs)))
    package = importlib.import_module(module)
    if kwargs.get('package'):
      kwargs.pop('package')
    setup_return = package.Setup(self, module, **kwargs)
    scheduler.runInS(10, self.refresh_firebase, job_id='FIREBASE_REFRESH_CORE')
    return setup_return

  def send_firebase(self, event: Event):
    ''' Send and event to firebase

    Args:
      event: event to be sent

    Returns:

    '''
    if self.components.get('service_firebase'):
      self.components['service_firebase'].push(event.source, event.event_action)

  def refresh_firebase(self, **kwargs):
    if self.firebase_enabled:
      self.components['service_firebase'].refresh_all()

  @asyncio.coroutine
  def async_send_event(self, event):
    logging.info('Received event: %s' % event)
    s = True
    fut = asyncio.Future(loop=self.loop)
    send_to = self._subscriptions.get_subscribers(event.source, event_action=event.event_action)
    for s in send_to:
      s &= yield from self._send_event(event, s, fut)
    self.send_firebase(event)
    return s

  def send_event(self, event: Event) -> Any:
    logging.info('Received event: %s' % event)
    fut = asyncio.Future(loop=self.loop)
    send_to = self._subscriptions.get_subscribers(event.source, event_action=event.event_action)
    for s in send_to:
      # asyncio.ensure_future(self._send_event(event, s, fut), loop=self.loop)
      try:
        self.components[s].event(event)
      except Exception as e:
        logging.error('Error sending event %s' % str(e))
        # self.loop.run_in_executor(None,self.components[s].event, event)
    self.update_current_state(event)
    self.send_firebase(event)
    return True

  @asyncio.coroutine
  def _send_event(self, event, ff_id, fut):
    result = self.components[ff_id].event(event)
    # fut.set_result(result)
    return result

  @asyncio.coroutine
  def async_send_request(self, request):
    fut = asyncio.Future(loop=self.loop)
    r = yield from self._send_request(request, fut)
    return r

  def send_request(self, request: Request) -> Any:
    fut = asyncio.Future(loop=self.loop)
    result = asyncio.ensure_future(self._send_request(request, fut), loop=self.loop)
    return result

  @asyncio.coroutine
  def _send_request(self, request, fut):
    result = self.components[request.ff_id].request(request)
    fut.set_result(result)
    return result

  def send_command(self, command, wait=False):
    if command.device not in self.components:
      return False
    try:
      if wait:
        fut = asyncio.run_coroutine_threadsafe(self.new_send_command(command, None, self.loop), self.loop)
        return fut.result(10)
      else:
        # asyncio.run_coroutine_threadsafe(self.send_command_no_wait(command, self.loop), self.loop)
        self.components[command.device].command(command)
        return True
    except Exception as e:
      logging.error(code='FF.COR.SEN.001')  # unknown error sending command
      logging.error(e)
      # TODO: Figure out how to wait for result
    return False

  async def new_send_command(self, command, fut, loop):
    fut = await asyncio.ensure_future(loop.run_in_executor(None, self.components[command.device].command, command))
    return fut

  async def send_command_no_wait(self, command, loop):
    # await asyncio.ensure_future(loop.run_in_executor(None, self.components[command.device].command, command))
    self.components[command.device].command(command)
    # loop.run_in_executor(None, self.components[command.device].command, command)
    return True

  @asyncio.coroutine
  def async_send_command(self, command):
    fut = asyncio.Future(loop=self.loop)
    result = yield from asyncio.ensure_future(self._send_command(command, fut), loop=self.loop)
    return result

  @asyncio.coroutine
  def _send_command(self, command, fut):
    if command.device in self.components:
      result = self.components[command.device].command(command)
      fut.set_result(result)
      return result
    logging.error(code='FF.COR._SE.001', args=(command.device))  # device not found %s
    return None

  def add_route(self, route, method, handler):
    app.router.add_route(method, route, handler)

  def add_get(self, route, handler, *args):
    app.router.add_get(route, handler)

  # TODO(zpriddy): Remove this function.
  def get_device_states(self, devices: set) -> dict:
    logging.warn('This is now deprecated.')
    current_state = {}
    if TIME in devices:
      devices.remove(TIME)
    if 'location' in devices:
      devices.remove('location')
    for device in devices:
      current_state[device] = self.components[device].get_all_request_values(True)
    return current_state

  def update_current_state(self, event: Event) -> None:
    """
    Update the global current state when a broadcast event is sent.

    Args:
      event (Event): the broadcast event.
    """
    # Do not update time.
    if event.source == TIME:
      return

    # Only look at broadcast events.
    if event.event_type != EVENT_TYPE_BROADCAST:
      return

    if event.source not in self.current_state:
      self.current_state[event.source] = {}

    self.current_state[event.source].update(event.event_action)

  def get_current_states(self):
    return self.current_state

  @property
  def components(self):
    return self._components

  @property
  def subscriptions(self):
    return self._subscriptions

  @property
  def firebase_enabled(self):
    return self._firebase_enabled

  @property
  def version(self):
    return VERSION
