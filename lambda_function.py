import json
import math
import random
import uuid
import logging
import datetime
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

USERID = 'kcepull'
PASSWORD = 'nvj@fbm_qpm4qux5UKR'
auth = (USERID,PASSWORD)

# URLs
MYICOMFORT_URL = "https://services.myicomfort.com/DBAcessService.svc/"

# Constants and arrays to convert from Lennox numbers to Alexa words
TEMPS = ['FAHRENHEIT', 'CELCIUS']
FAN_MODES = ['AUTO','ON','CIRCULATE']
HVAC_MODES = ['OFF', 'HEAT','COOL', 'AUTO'] 


def discover():
    '''
    Retrieves list of devices in account.
    Returns one endpoint for each thermostat.
    If a thermostat has multiple zones, returns one endpoint for each zone.
    '''
    
    # Build response
    discovery_response = AlexaResponse(namespace='Alexa.Discovery', name='Discover.Response')
    # Create the response and add the capabilities.
    capability_alexa = discovery_response.create_payload_endpoint_capability()
    capability_alexa_temperaturesensor = discovery_response.create_payload_endpoint_capability(
        interface='Alexa.TemperatureSensor',
        supported=[{'name': 'temperature'}],
        retrievable=True)
    capability_alexa_thermostatcontroller = discovery_response.create_payload_endpoint_capability(
        interface='Alexa.ThermostatController',
        supported=[
            {'name': 'targetSetpoint'},
            {'name': 'lowerSetpoint'},
            {'name': 'upperSetpoint'},
            {'name': 'thermostatMode'}
            ],
        version='3.2',
        retrievable=True,
        configuration = {'supportedModes' : ["OFF","HEAT","COOL","AUTO","ECO"]})
    capability_alexa_endpointhealth = discovery_response.create_payload_endpoint_capability(
        interface='Alexa.EndpointHealth',
        supported=[{'name': 'connectivity'}],
        version='3.2',
        retrievable=True)

    # Get list of thermostats        
    r = requests.get(MYICOMFORT_URL + "GetSystemsInfo?userid=" + USERID, auth=auth)
    # TODO - error checking
    response = json.loads(r.text)
    systemsInfo = response['Systems']
    logger.debug(systemsInfo)
        
    # Loop through thermostats
    for system in systemsInfo:
        # Get TStat info to see how many zones
        r = requests.get(MYICOMFORT_URL + "GetTStatInfoList?gatewaysn=" + system['Gateway_SN'] + "&TempUnit=&Cancel_Away=-1", auth=auth)
        response = json.loads(r.text)
        tStatInfo = response['tStatInfo']
        logger.debug(tStatInfo)
        # Loop through zones
        for zone in tStatInfo:
            if zone['Zone_Enabled'] == 1:
                discovery_response.add_payload_endpoint(
                    friendly_name = zone['Zone_Name'] if zone['Zones_Installed'] > 1 else system['System_Name'],
                    endpoint_id = zone['GatewaySN'] + ":" + str(zone['Zone_Number']),
                    manufacturer_name = 'Lennox',
                    description = 'Wi-Fi Thermostat by Lennox',
                    display_categories = ['THERMOSTAT','TEMPERATURE_SENSOR'],
                    capabilities = [capability_alexa, capability_alexa_endpointhealth, capability_alexa_temperaturesensor, capability_alexa_thermostatcontroller],
                    additionalAttributes = {
                        'serialNumber': system['Gateway_SN'],
                        'firmwareVersion': system['Firmware_Ver'],
                        'customIdentifier': str(system['SystemID'])
                        }
                    )
    response = discovery_response.get()
    logger.debug(response)
    return send_response(response)

def getAlexaResponse(endpointId, name='Response'):
    '''
    Returns an Alexa response object with the thermostat's current state
    '''
    tStatInfo = LennoxWiFi(endpointId, auth).getTStatInfo()
    temp_units = TEMPS[int(tStatInfo['Pref_Temp_Units'])]

    # Build response
    alexa_response = AlexaResponse(namespace='Alexa', name=name, endpoint_id=endpointId)
    alexa_response.add_context_property(namespace='Alexa.TemperatureSensor', name='temperature', value={'value':tStatInfo['Indoor_Temp'], 'scale':temp_units})
    alexa_response.add_context_property(namespace='Alexa.ThermostatController', name='thermostatMode', value={'value':HVAC_MODES[tStatInfo['Operation_Mode']]})
    # if mode is HEAT or COOL, return a single setpoint. Otherwise, return upper and lower setpoints.
    if tStatInfo['Operation_Mode'] == HVAC_MODES.index('COOL'):
        alexa_response.add_context_property(namespace='Alexa.ThermostatController', name='targetSetpoint', value={'value':tStatInfo['Cool_Set_Point'], 'scale':temp_units})
    elif tStatInfo['Operation_Mode'] == HVAC_MODES.index('HEAT'):
        alexa_response.add_context_property(namespace='Alexa.ThermostatController', name='targetSetpoint', value={'value':tStatInfo['Heat_Set_Point'], 'scale':temp_units})
    else:
        alexa_response.add_context_property(namespace='Alexa.ThermostatController', name='lowerSetpoint', value={'value':tStatInfo['Heat_Set_Point'], 'scale':temp_units})
        alexa_response.add_context_property(namespace='Alexa.ThermostatController', name='upperSetpoint', value={'value':tStatInfo['Cool_Set_Point'], 'scale':temp_units})
        
    return alexa_response
    

def reportState(endpointId):
    '''
    Returns the state of the thermostat.
    '''
    return send_response(getAlexaResponse(endpointId, 'StateReport').get())
    
def setTemperature(endpointId, firstSetpoint, secondSetpoint=None):
    '''
    Sets temperature to value(s).
    If one value is passed in, attempts to calculate whether it should be the upper or lower setpoint.
    If two values are passed in, sets both lower and upper.
    Assumes 'scale' passed in is what thermostat wants. Should we detect and convert?
    '''

    # Get current values for fields we have to pass back
    tStat = LennoxWiFi(endpointId, auth)
    tStatInfo = tStat.getTStatInfo()
    logger.debug(f'Current set points: {tStatInfo['Heat_Set_Point']} and {tStatInfo['Cool_Set_Point']}')
    
    if secondSetpoint is None:
        # One value passed in.
        # If mode is 'COOL', then set upper.
        # If mode is 'HEAT', then set lower.
        # Otherwise (in AUTO), make a best guess as to which set point needs to change.
        # If the requested temp is above the current upper setpoint, then set the upper setpoint to the new temp.
        # If the requested temp is below the current lower setpoint, then set the lower setpoint to the new temp.
        # If the requested temp is between the current setpoints, see which one the current
        # temperature is nearest, then change that one. The assumption being that the current
        # temp is close to the desired value, and this 'delta' is just a tweak.
        # E.g. If the current setpoint range is 64 and 71, and the current temp is 65, assume we 
        # are heating, so change the lower setpoint. 
        logger.debug('Requested setpoint = %i' % firstSetpoint)
        if tStatInfo['Operation_Mode'] == HVAC_MODES.index('COOL'):
            logger.debug(f'COOL: Setting upper setpoint to {firstSetpoint}')
            tStat.setTStatInfo(upperSetpoint=firstSetpoint)
        elif tStatInfo['Operation_Mode'] == HVAC_MODES.index('HEAT'):
            logger.debug(f'HEAT: Setting lower setpoint to {firstSetpoint}')
            tStat.setTStatInfo(lowerSetpoint=firstSetpoint)
        else: # AUTO
            if firstSetpoint <= tStatInfo['Heat_Set_Point']:
                # Requested temp is below lower set point, so change it.
                logger.debug(f'AUTO: {firstSetpoint} is below lower setpoint. Setting lower setpoint to {firstSetpoint}')
                tStat.setTStatInfo(lowerSetpoint=firstSetpoint)
            elif firstSetpoint >= tStatInfo['Cool_Set_Point']:
                # Requested temp is above upper set point, to change it.
                logger.debug(f'AUTO: {firstSetpoint} is above upper setpoint. Setting upper setpoint to {firstSetpoint}')
                tStat.setTStatInfo(upperSetpoint=firstSetpoint)
            else:
                # Requested temp is between current setpoints. 
                # Find which setpoint current temp is nearest and change it
                midpoint = (tStatInfo['Heat_Set_Point'] + tStatInfo['Cool_Set_Point']) / 2.0
                logger.debug(f'Midpoint = {midpoint}')
                logger.debug(f'Current temp = {tStatInfo['Indoor_Temp']}')
                if tStatInfo['Indoor_Temp'] < midpoint:
                    logger.debug(f'AUTO: Setting lower setpoint to {firstSetpoint}')
                    tStat.setTStatInfo(lowerSetpoint=firstSetpoint)
                else:
                    logger.debug(f'AUTO: Setting upper setpoint to {firstSetpoint}')
                    tStat.setTStatInfo(upperSetpoint=firstSetpoint)
    else:
        # two values passed in
        tStat.setTStatInfo(lowerSetpoint=firstSetpoint, upperSetpoint=secondSetpoint)
        logger.debug(f'New set points: {firstSetpoint} and {secondSetpoint}')
    
    return send_response(getAlexaResponse(endpointId).get())
    
def adjustTemperature(endpointId, delta):
    '''
    Adjusts the thermostat by 'delta' degrees (plus or minus).
    '''

    # Get current values for fields we have to pass back
    tStat = LennoxWiFi(endpointId, auth)
    tStatInfo = tStat.getTStatInfo()
    upperSetpoint = tStatInfo['Cool_Set_Point']
    lowerSetpoint = tStatInfo['Heat_Set_Point']
    logger.debug(f'Current set points: {lowerSetpoint} and {upperSetpoint}')
    
    # If system is in COOL or HEAT mode, set corresponding set point.
    # Otherwise (in AUTO), if delta is negative, adjusts upper (cool) setpoint, otherwise adjust lower (heat) setpoint.
    logger.debug('Requested setpoint adjustment = %i' % delta)
    if tStatInfo['Operation_Mode'] == 'COOL':
        upperSetpoint += delta
        tStat.setTStatInfo(upperSetpoint=upperSetpoint)
    elif tStatInfo['Operation_Mode'] == 'HEAT':
        lowerSetpoint += delta
        tStat.setTStatInfo(lowerSetpoint=lowerSetpoint)
    else:
        # If delta is negative, adjust upper. Otherwise, lower.
        if delta > 0:
            lowerSetpoint += delta
            # if difference between upper and lower is < 3 degrees, make it 3.
            if upperSetpoint - lowerSetpoint < 3:
                upperSetpoint = lowerSetpoint + 3
            logger.debug(f'AUTO: Adjusted heat/lower setpoint by {delta}')
        else:
            upperSetpoint += delta
            # if difference between upper and lower is < 3 degrees, make it 3.
            if upperSetpoint - lowerSetpoint < 3:
                lowerSetpoint = upperSetpoint - 3
            logger.debug(f'AUTO: Adjusted cool/upper setpoint by {delta}')
        # Set both (in case both changed)
        tStat.setTStatInfo(lowerSetpoint=lowerSetpoint, upperSetpoint=upperSetpoint)
    logger.debug(f'New set points: {lowerSetpoint} and {upperSetpoint}')

    return send_response(getAlexaResponse(endpointId).get())
    
def setOperatingMode(endpointId, mode):
    '''
    Sets mode (heat, cool, away/eco, off, auto).
    'mode' is one of "OFF", "HEAT", "COOL", "AUTO", or "ECO" (which sets Away mode)
    '''
    tStat = LennoxWiFi(endpointId, auth)
    tStatInfo = tStat.getTStatInfo()
    logger.debug('Current operating mode: %s' % HVAC_MODES[tStatInfo['Operation_Mode']])
    tStat.setTStatInfo(operating_mode=HVAC_MODES.index(mode))
    logger.debug('Set operating mode to %s' % mode)

    return send_response(getAlexaResponse(endpointId).get())
    

def lambda_handler(request, context):

    # Dump the request for logging - check the CloudWatch logs.
    print('lambda_handler request  -----')
    print(json.dumps(request))


    if context is not None:
        print('lambda_handler context  -----')
        print(context)

    # Validate the request is an Alexa smart home directive.
    if 'directive' not in request:
        alexa_response = AlexaResponse(
            name='ErrorResponse',
            payload={'type': 'INVALID_DIRECTIVE',
                     'message': 'Missing key: directive, Is the request a valid Alexa Directive?'})
        return send_response(alexa_response.get())
    directive = request['directive']

    # Check the payload version.
    payload_version = directive['header']['payloadVersion']
    if float(payload_version) < 3.0:
        alexa_response = AlexaResponse(
            name='ErrorResponse',
            payload={'type': 'INTERNAL_ERROR',
                     'message': 'This skill only supports Smart Home API version 3'})
        return send_response(alexa_response.get())

    # Crack open the request to see the request.
    name = directive['header']['name']
    namespace = directive['header']['namespace']
    auth = (USERID, PASSWORD)

    # Handle the incoming request from Alexa based on the namespace.
    if namespace == 'Alexa.Authorization':
        if name == 'AcceptGrant':
            # Note: This example code accepts any grant request.
            # In your implementation, invoke Login With Amazon with the grant code to get access and refresh tokens.
            grant_code = directive['payload']['grant']['code']
            grantee_token = directive['payload']['grantee']['token']
            auth_response = AlexaResponse(namespace='Alexa.Authorization', name='AcceptGrant.Response')
            return send_response(auth_response.get())

    if namespace == 'Alexa.Discovery':
        if name == 'Discover':
            return discover()
    
    if namespace == 'Alexa':
        if name == 'ReportState':
            return reportState(directive['endpoint']['endpointId'])

    if namespace == 'Alexa.ThermostatController':
        if name == 'SetTargetTemperature':
            if 'targetSetpoint' in directive['payload'].keys():
                return setTemperature(directive['endpoint']['endpointId'], directive['payload']['targetSetpoint']['value'])
            else:
                return setTemperature(directive['endpoint']['endpointId'], directive['payload']['lowerSetpoint']['value'], directive['payload']['upperSetpoint']['value'])
        if name == 'AdjustTargetTemperature':
            return adjustTemperature(directive['endpoint']['endpointId'], directive['payload']['targetSetpointDelta']['value'])            
        if name == 'SetThermostatMode':
            return setOperatingMode(directive['endpoint']['endpointId'], directive['payload']['thermostatMode']['value'])
        if name == 'ResumeSchedule':
            return None;

        
# Send the response
def send_response(response):
    print('lambda_handler response -----')
    print(json.dumps(response))
    return response

# Make the call to your device cloud for control
def update_device_state(endpoint_id, state, value):
    attribute_key = state + 'Value'
    # result = stubControlFunctionToYourCloud(endpointId, token, request);
    return True

# Datetime format for timeOfSample is ISO 8601, `YYYY-MM-DDThh:mm:ssZ`.
def get_utc_timestamp(seconds=None):
    return datetime.now(timezone.utc).isoformat()

class AlexaResponse:

    def __init__(self, **kwargs):

        self.context_properties = []
        self.payload_endpoints = []

        # Set up the response structure.
        self.context = {}
        self.event = {
            'header': {
                'namespace': kwargs.get('namespace', 'Alexa'),
                'name': kwargs.get('name', 'Response'),
                'messageId': str(uuid.uuid4()),
                'payloadVersion': kwargs.get('payload_version', '3')
            },
            'endpoint': {
                "scope": {
                    "type": "BearerToken",
                    "token": kwargs.get('token', 'INVALID')
                },
                "endpointId": kwargs.get('endpoint_id', 'INVALID')
            },
            'payload': kwargs.get('payload', {})
        }

        if 'correlation_token' in kwargs:
            self.event['header']['correlation_token'] = kwargs.get('correlation_token', 'INVALID')

        if 'cookie' in kwargs:
            self.event['endpoint']['cookie'] = kwargs.get('cookie', '{}')

        # No endpoint property in an AcceptGrant or Discover request.
        if self.event['header']['name'] == 'AcceptGrant.Response' or self.event['header']['name'] == 'Discover.Response':
            self.event.pop('endpoint')

    def add_context_property(self, **kwargs):
        if len(self.context_properties) == 0:
            self.context_properties.append(self.create_context_property())
        self.context_properties.append(self.create_context_property(**kwargs))


    def add_cookie(self, key, value):

        if "cookies" in self is None:
            self.cookies = {}

        self.cookies[key] = value

    def add_payload_endpoint(self, **kwargs):
        self.payload_endpoints.append(self.create_payload_endpoint(**kwargs))


    def create_context_property(self, **kwargs):
        return {
            'namespace': kwargs.get('namespace', 'Alexa.EndpointHealth'),
            'name': kwargs.get('name', 'connectivity'),
            'value': kwargs.get('value', {'value': 'OK'}),
            'timeOfSample': get_utc_timestamp(),
            'uncertaintyInMilliseconds': kwargs.get('uncertainty_in_milliseconds', 0)
        }

    def create_payload_endpoint(self, **kwargs):
        # Return the proper structure expected for the endpoint.
        # All discovery responses must include the additionalAttributes
        additionalAttributes = {
            'manufacturer': kwargs.get('manufacturer', 'Sample Manufacturer'),
            'model': kwargs.get('model_name', 'Sample Model'),
            'serialNumber': kwargs.get('serial_number', 'U11112233456'),
            'firmwareVersion': kwargs.get('firmware_version', '1.24.2546'),
            'softwareVersion': kwargs.get('software_version', '1.036'),
            'customIdentifier': kwargs.get('custom_identifier', 'Sample custom ID')
        }

        endpoint = {
            'capabilities': kwargs.get('capabilities', []),
            'description': kwargs.get('description', 'Smart Home Tutorial: Virtual smart light bulb'),
            'displayCategories': kwargs.get('display_categories', ['LIGHT']),
            'endpointId': kwargs.get('endpoint_id', 'endpoint_' + "%0.6d" % random.randint(0, 999999)),
            'friendlyName': kwargs.get('friendly_name', 'Sample light'),
            'manufacturerName': kwargs.get('manufacturer_name', 'Sample Manufacturer')
        }

        endpoint['additionalAttributes'] = kwargs.get('additionalAttributes', additionalAttributes)
        if 'cookie' in kwargs:
            endpoint['cookie'] = kwargs.get('cookie', {})

        return endpoint

    def create_payload_endpoint_capability(self, **kwargs):
        # All discovery responses must include the Alexa interface
        capability = {
            'type': kwargs.get('type', 'AlexaInterface'),
            'interface': kwargs.get('interface', 'Alexa'),
            'version': kwargs.get('version', '3')
        }
        configuration = kwargs.get('configuration', None)
        if configuration:
            capability['configuration'] = configuration
        supported = kwargs.get('supported', None)
        if supported:
            capability['properties'] = {}
            capability['properties']['supported'] = supported
            capability['properties']['proactivelyReported'] = kwargs.get('proactively_reported', False)
            capability['properties']['retrievable'] = kwargs.get('retrievable', False)
        return capability

    def get(self, remove_empty=True):

        response = {
            'context': self.context,
            'event': self.event
        }

        if len(self.context_properties) > 0:
            response['context']['properties'] = self.context_properties

        if len(self.payload_endpoints) > 0:
            response['event']['payload']['endpoints'] = self.payload_endpoints

        if remove_empty:
            if len(response['context']) < 1:
                response.pop('context')

        return response

    def set_payload(self, payload):
        self.event['payload'] = payload

    def set_payload_endpoint(self, payload_endpoints):
        self.payload_endpoints = payload_endpoints

    def set_payload_endpoints(self, payload_endpoints):
        if 'endpoints' not in self.event['payload']:
            self.event['payload']['endpoints'] = []

        self.event['payload']['endpoints'] = payload_endpoints


class LennoxWiFi:
    def __init__(self, endpointId, auth):
        '''
        Creates an instance of LennoxWiFi representing a thermostat and zone.
        endpointID is the form '<serialNumber>:<zoneId>'
        auth is a tuple of (userid,password)
        '''
        (self.gatewaysn, self.zone_num) = endpointId.split(':')
        self.auth = auth
        
    def getTStatInfo(self):
        '''
        Returns a tStatInfo object about the thermostat/zone.
        '''
        r = requests.get(MYICOMFORT_URL + "GetTStatInfoList?gatewaysn=" + self.gatewaysn + "&tempunit=&Cancel_Away=-1&Zone_number=" + self.zone_num, auth=self.auth)
        # TODO error handling!
        logger.debug(r.text)
        response = json.loads(r.text)
        self.tStatInfo = response['tStatInfo'][0]
        return self.tStatInfo

    def setTStatInfo(self, operating_mode=None, lowerSetpoint=None, upperSetpoint=None, fan_mode=None, temp_units=None):
        '''
        Sets thermostat settings based on passed-in values.
        '''

        logger.debug('Operating Mode: %s, Lower Setpoint: %s, Upper Setpoint: %s, Fan Mode: %s, Temp Units: %s' % (str(operating_mode), str(lowerSetpoint), str(upperSetpoint), str(fan_mode), str(temp_units)))
        headers = {
            'Content-Type': 'application/json'
        }
        # If we don't have current state, get it.
        if not self.tStatInfo:
            self.getTStatInfo()
        logger.debug('--- self.tStatInfo:')
        logger.debug(self.tStatInfo)
        
        # Build new data object
        data = {
            'GatewaySN' : self.tStatInfo['GatewaySN'],
            'Zone_Number' : self.tStatInfo['Zone_Number'],
            'Cool_Set_Point' : upperSetpoint if upperSetpoint is not None else self.tStatInfo['Cool_Set_Point'],
            'Heat_Set_Point' : lowerSetpoint if lowerSetpoint is not None else self.tStatInfo['Heat_Set_Point'],
            'Fan_Mode' : fan_mode if fan_mode is not None else self.tStatInfo['Fan_Mode'],
            'Operation_Mode' : operating_mode if operating_mode is not None else self.tStatInfo['Operation_Mode'],
            'Pref_Temp_Units' : temp_units if temp_units is not None else self.tStatInfo['Pref_Temp_Units']
        }
                
        # TODO error handling
        logger.debug(data)
        r = requests.put(MYICOMFORT_URL + "SetTStatInfo", json=data, auth=self.auth, headers=headers)
        logger.debug(r.text)
