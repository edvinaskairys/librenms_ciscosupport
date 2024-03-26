"""take information from librenms and then query cisco support apis for information"""
from dotenv import dotenv_values
import datetime
import time
import re
import json
import urllib3
import requests


#librenms details
LIBRE_BASE_URL = 'https://libre.XXX.zone/api/v0/'
LIBRE_AUTH_TOKEN = dotenv_values('.env')['LIBRE_AUTH_TOKEN']
LIBRE_HEADER = {'x-auth-token': LIBRE_AUTH_TOKEN}

#Cisco Support API credentials
CISCO_TOKEN_URL = "https://id.cisco.com/oauth2/default/v1/token"
CISCO_CLIENT_ID = dotenv_values('.env')['CISCO_CLIENT_ID']
CISCO_CLIENT_SECRET = dotenv_values('.env')['CISCO_CLIENT_SECRET']

#manual soft suggestion, to use when there's no suggested version found
#https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus9000/sw/recommended_release/b_Minimum_and_Recommended_Cisco_NX-OS_Releases_for_Cisco_Nexus_9000_Series_Switches.html
manual_soft = {'ASR-9001-S': '6.9.2',
               'ASR-9001': '6.9.2',
               'WS-C3560E-24TD-E': '15.2.4E10',
               'N9K-C9372PX-E': '9.3(13)',
               'N9K-C93180YC-EX': '10.3(4a)',
               'N9K-C9364C-GX': '10.3(4a)',
               'N9K-C9336C-FX2': '10.3(4a)',
               'N9K-C93180YC-FX3': '10.3(4a)',
               'N9K-C93240YC-FX2': '10.3(4a)',
               'N9K-C93360YC-FX2': '10.3(4a)'}

CISCO_CURRENT_TOKEN = None
CISCO_TOKEN_EXPIRATION_TIME = None


def get_cisco_api_access_token():
    """Get token from Cisco api"""
    global CISCO_CURRENT_TOKEN, CISCO_TOKEN_EXPIRATION_TIME
    # Check if the current token is still valid
    if CISCO_CURRENT_TOKEN and CISCO_TOKEN_EXPIRATION_TIME > datetime.datetime.now():
        return CISCO_CURRENT_TOKEN
    response = requests.post(
        CISCO_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(CISCO_CLIENT_ID, CISCO_CLIENT_SECRET),)
    response.raise_for_status()

    # Update the current token and its expiration time
    CISCO_CURRENT_TOKEN = response.json()["access_token"]
    CISCO_TOKEN_EXPIRATION_TIME = datetime.datetime.now() + datetime.timedelta(seconds=response.json()["expires_in"])
    return CISCO_CURRENT_TOKEN


def send_query(url):
    """function to send query"""
    cisco_api_token = get_cisco_api_access_token()
    headers = {'Accept': 'application/json', 'Authorization': f"Bearer {cisco_api_token}"}
    payload = {}
    req = requests.get(url, headers=headers, timeout=10, data=payload)
    req.raise_for_status()
    return req.json()


def parse_cisco_version(version):
    """parse cisco versions"""
    # Extract numeric parts and optional suffix
    numeric_parts = re.findall(r'\d+', version)
    suffix = re.findall(r'[a-zA-Z].*', version)
    # Convert numeric parts to integers
    numeric_parts = tuple(map(int, numeric_parts))
    # Include the suffix in the tuple for comparison, default to empty string if none
    full_version_tuple = numeric_parts + tuple(suffix if suffix else '')
    return full_version_tuple


def seconds_to_years_months_days(seconds):
    """convert from seconds to years and months"""
    seconds_in_a_year = 365.25 * 24 * 60 * 60  # accounting for leap years
    seconds_in_a_month = 30.44 * 24 * 60 * 60  # average month length
    seconds_in_a_day = 24 * 60 * 60
    years = seconds // seconds_in_a_year
    years = int(years)
    remaining_seconds = seconds % seconds_in_a_year
    months = remaining_seconds // seconds_in_a_month
    months = int(months)
    remaining_seconds %= seconds_in_a_month
    days = remaining_seconds // seconds_in_a_day
    days = int(days)
    return f"{years} yrs, {months} mnths, {days} days"


def libre_get(url):
    """General GET helper function, pass sub-url to append to base URL for request """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    #return requests.get('{0}{1}'.format(LIBRE_BASE_URL, url), headers=LIBRE_HEADER, verify=False)
    return requests.get(f"{LIBRE_BASE_URL}{url}", headers=LIBRE_HEADER, verify=False)


def get_group_id():
    """Get all devices and XXX DC group devices IDs and filter them."""
    all_device_list = libre_get('devices/').json()['devices']
    group_ids = libre_get('devicegroups/8').json()['devices']
    device_dict = {}
    for devices in all_device_list:
        for group in group_ids:
            if str(devices['device_id']) == str(group['device_id']):
                device_dict[devices['hostname']] = {}
                device_dict[devices['hostname']]['hardware'] = devices['hardware']
                device_dict[devices['hostname']]['platform'] = devices['sysDescr'].split()[0]
                device_dict[devices['hostname']]['type'] = devices['type']
                device_dict[devices['hostname']]['serial'] = devices['serial']
                device_dict[devices['hostname']]['uptime'] = seconds_to_years_months_days(devices['uptime'])
                device_dict[devices['hostname']]['current_version'] = devices['version']
    return device_dict


def inventory_list(hostname):
    """Get info from devices inventory"""
    #inventory = libre_get('inventory/{0}'.format(hostname)).json()['inventory']
    inventory = libre_get(f'inventory/{hostname}').json()['inventory']
    dict_spec = {}
    for device in inventory:
        #skip nodes without primary Model name or serial number (that would be for example STACKED devices)
        if device['entPhysicalModelName'] != '' and device['entPhysicalSerialNum'] != '':
            dict_spec[hostname] = {}
            dict_spec[hostname]['hardware'] = device['entPhysicalModelName']
            dict_spec[hostname]['serial'] = device['entPhysicalSerialNum']
            #break after first inventory iteration, because only there mandatory info exists.
            break
    return dict_spec


def libre_dicts():
    """Merge filtered dict of dicts and inventory dict."""
    device_dict = get_group_id()
    new_dict = {}
    for hostname in device_dict:
        #filter dcvpnl nodes, because no inventory is found there.
        if 'dcvpnl' not in hostname:
            new_dict.update(inventory_list(hostname))

    merged_dict = {}
    for device_item, device_details in device_dict.items():
        merged_dict[device_item] = device_details
        if new_dict.get(device_item):
            merged_dict[device_item]['hardware'] = new_dict[device_item]['hardware']
            merged_dict[device_item]['serial'] = new_dict[device_item]['serial']
    #print(merged_dict)
    return merged_dict


def software_suggestion(items):
    """Api for software suggestion"""
    #lowered from 10 to 8 because some version data was not retrieved.
    max_items = 8
    api_url = 'https://apix.cisco.com/software/suggestion/v2/suggestions/releases/productIds/{}?pageIndex={}'
    start_index = 0
    end_index = max_items
    records = []
    while start_index <= len(items) - 1:
        page_index = 1
        pagination = True
        while pagination:
            url = api_url.format((',').join(items[start_index:end_index]),page_index)
            #print(url)
            resp = send_query(url)
            #print(resp)
            if resp.get('productList'):
                records = records + resp['productList']
            if page_index >= int(resp['paginationResponseRecord']['lastIndex']):
                pagination = False
            else:
                page_index += 1
            # Play nice with Cisco API's and rate limit your queries
            time.sleep(0.5)
        start_index = end_index
        end_index += max_items

    plat_soft_temp = {}
    for I in records:
        if I['product']['basePID'] == 'NCS-5501-SE' and 'Network Convergence System 5501-SE' in I[
                'product']['productName'] and 'IOS XR Software' in I['product']['softwareType']:
            plat_soft_temp[I['product']['basePID']] = I['product']['basePID']
            temp_version_list = []
            for version in I['suggestions']:
                temp_version_list.append(version['releaseFormat1'])
            plat_soft_temp[I['product']['basePID']] = max(temp_version_list, key=parse_cisco_version)
        #default filter - to exclude not needed data like related to NBAR2, ACI, KICK Start.
        elif 'NBAR2' not in I['product']['softwareType'] and 'Software-ACI' not in I['product'][
                'softwareType'] and 'Kick Start' not in I['product']['softwareType'] and 'SD-WAN' not in I['product'][
                     'productName'] and I['product']['basePID']:
            plat_soft_temp[I['product']['basePID']] = I['product']['basePID']
            temp_version_list = []
            for version in I['suggestions']:
                temp_version_list.append(version['releaseFormat1'])
            plat_soft_temp[I['product']['basePID']] = max(temp_version_list, key=parse_cisco_version)

    #print(len(plat_soft_temp))
    return plat_soft_temp


def hardware_eox(items):
    """get hardware end of support date"""
    max_items = 20
    api_url = 'https://apix.cisco.com/supporttools/eox/rest/5/EOXByProductID/{}/{}'
    start_index = 0
    end_index = max_items
    records = []
    while start_index <= len(items) - 1:
        page_index = 1
        pagination = True
        while pagination:
            url = api_url.format(page_index,(',').join(items[start_index:end_index]))
            #print(url)
            resp = send_query(url)
            if resp.get('EOXRecord'):
                records = records + resp['EOXRecord']
            if page_index >= resp['PaginationResponseRecord']['LastIndex']:
                pagination = False
            else:
                page_index += 1
            # Play nice with Cisco API's and rate limit your queries
            time.sleep(0.5)

        start_index = end_index
        end_index += max_items

    plat_eox_temp = {}
    for I in records:
        if I['EOLProductID'] != '':
            plat_eox_temp[I['EOLProductID']] = I['LastDateOfSupport']['value']
    return plat_eox_temp


def serial_support(items):
    """get serial numbers"""
    max_items = 70
    api_url = 'https://apix.cisco.com/sn2info/v2/coverage/status/serial_numbers/{}'
    start_index = 0
    end_index = max_items
    records = []
    while start_index <= len(items) - 1:
        url = api_url.format((',').join(items[start_index:end_index]))
        resp = send_query(url)
        records = records + resp['serial_numbers']
        # Play nice with Cisco API's and rate limit your queries
        time.sleep(0.5)
        start_index = end_index
        end_index += max_items

    plat_contract_temp = {}
    for I in records:
        plat_contract_temp[I['sr_no']] = {}
        plat_contract_temp[I['sr_no']]['is_covered'] = I['is_covered']
        plat_contract_temp[I['sr_no']]['coverage_end_date'] = I['coverage_end_date']
        if I['is_covered'] == 'YES':
            if I['coverage_end_date'] != '':
                plat_contract_temp[I['sr_no']]['result'] = 'Yes, till' + " " + I['coverage_end_date']
            elif I['coverage_end_date'] == '':
                plat_contract_temp[I['sr_no']]['result'] = 'Yes'
        elif I['is_covered'] == 'NO':
            plat_contract_temp[I['sr_no']]['result'] = 'No'
    return plat_contract_temp


def main():
    """aggregate all data"""
    libre_inv = libre_dicts()
    #print(libre_inv)
    dev_list = []
    #make list with only hardware info
    for details in libre_inv.values():
        if details['platform'] == 'Cisco':
            dev_list.extend([details['hardware']])

    #take only unique hw values, and see software suggestion and eox dates
    plat_soft = software_suggestion(list(set(dev_list)))
    plat_eox = hardware_eox(list(set(dev_list)))
    device_serial_info = serial_support([II['serial'] for I, II in libre_inv.items() if II['platform'] == 'Cisco'])

    #merge libre dict of dicts with software suggestion dict
    final_dict = {}
    for device_item, device_details in libre_inv.items():
        final_dict[device_item] = device_details
        final_dict[device_item]["read_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if plat_soft.get(final_dict[device_item]["hardware"]):
            final_dict[device_item]["recommended"] = plat_soft[final_dict[device_item]["hardware"]]
        elif manual_soft.get(final_dict[device_item]["hardware"]):
            final_dict[device_item]["recommended"] = \
                manual_soft[final_dict[device_item]["hardware"]] + " " + "*"
        elif device_details["hardware"] not in manual_soft:
            final_dict[device_item]["recommended"] = "Not found"
        if plat_eox.get(final_dict[device_item]["hardware"]):
            final_dict[device_item]["end_of_support_date"] = plat_eox[final_dict[device_item]["hardware"]]
        elif device_details["hardware"] not in plat_eox:
            final_dict[device_item]["end_of_support_date"] = "Not announced"
        if device_details['platform'] == 'Cisco':
            final_dict[device_item]["device_contract"] = \
                device_serial_info[final_dict[device_item]['serial']]['result']
    print(final_dict)

# Convert and write JSON object to file
    with open("/root/inventory_project/dict.json", "w", encoding="utf-8") as outfile:
        json.dump(final_dict, outfile)


if __name__ == "__main__":
    main()
