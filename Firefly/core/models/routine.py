# -*- coding: utf-8 -*-
# @Author: Zachary Priddy
# @Date:   2016-04-11 09:54:21
# @Last Modified by:   Zachary Priddy
# @Last Modified time: 2016-04-13 00:49:55
#
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json
import logging

class Routine(object):
  def __init__(self, configJson):
    config = json.loads(configJson)

    self._name = config.get('id')
    self._mode = config.get('mode')
    self._triggers = config.get('triggers')
    self._devices = config.get('devices')
    self._scheduling = config.get('scheduling')

    self._listen = [x.keys()[0] for x in self._triggers]

  def __str__(self):
    return ('<ROUTINE>\nName: ' + str(self._name) + 
      '\nMode: ' + str(self._mode) + 
      '\nListen: ' + str(self._listen) + 
      '\n<END ROUTINE>')

  @property
  def listen(self):
      return self._listen

  @property
  def mode(self):
      return self._mode

  @property
  def triggers(self):
      return self._triggers

  @property
  def devices(self):
      return self._devices

  @property
  def scheduling(self):
      return self._scheduling


  def event(self, event):
    from core.firefly_api import send_request, event_message
    from core.models.request import Request as FFRequest
    logging.debug('ROUTINE: Receving Event In: ' + str(self._name))

    for trigger in self._triggers:
      if event.deviceID in trigger.keys():
        print 'Device in triggers'
        should_trigger = True
        for device, state in trigger.iteritems():
          status = send_request(FFRequest(device,state.keys()[0]))
          if str(status) == str(state.values()[0]):
            pass
          else:
            should_trigger = False

        if should_trigger:
          print '******************* TRIGGER *******************'
          event_message(self._name,"Routine Triggered")
          print str(self._mode)

  


  
  
  
  