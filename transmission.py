import urllib2
import simplejson as json
import httplib
import re
import time

# optional features provided by non-standard modules
features = {'dns':False, 'geoip':False}
try:   import adns; features['dns'] = True     # resolve IP to host name
except ImportError: features['dns'] = False

try:   import GeoIP; features['geoip'] = True  # show country peer seems to be in
except ImportError:  features['geoip'] = False

# Handle communication with Transmission server.
class TransmissionRequest:
    def __init__(self, host, port, method=None, tag=None, arguments=None):
        self.url = 'http://%s:%d/transmission/rpc' % (host, port)
        self.open_request  = None
        self.last_update   = 0
        if method and tag:
            self.set_request_data(method, tag, arguments)

    def set_request_data(self, method, tag, arguments=None):
        request_data = {'method':method, 'tag':tag}
        if arguments: request_data['arguments'] = arguments
#        self.debug(repr(request_data) + "\n")
        self.http_request = urllib2.Request(url=self.url, data=json.dumps(request_data))

    @staticmethod
    def _html2text(str):
        str = re.sub(r'</h\d+>', "\n", str)
        str = re.sub(r'</p>', ' ', str)
        str = re.sub(r'<[^>]*?>', '', str)
        return str

    def send_request(self):
        """Ask for information from server OR submit command."""

        try:
            self.open_request = urllib2.urlopen(self.http_request)
        except AttributeError:
            # request data (http_request) isn't specified yet -- data will be available on next call
            pass
        except httplib.BadStatusLine, msg:
            # server sends something httplib doesn't understand.
            # (happens sometimes with high cpu load[?])
            pass  
        except urllib2.HTTPError, msg:
            msg = self._html2text(str(msg.read()))
            m = re.search('X-Transmission-Session-Id:\s*(\w+)', msg)
            try: # extract session id and send request again
                self.http_request.add_header('X-Transmission-Session-Id', m.group(1))
                self.send_request()
            except AttributeError: # a real error occurred
                quit(str(msg) + "\n", CONNECTION_ERROR)
        except urllib2.URLError, msg:
            try:
                reason = msg.reason[1]
            except IndexError:
                reason = str(msg.reason)
            quit("Cannot connect to %s: %s\n" % (self.http_request.host, reason), CONNECTION_ERROR)

    def get_response(self):
        """Get response to previously sent request."""

        if self.open_request == None:
            return {'result': 'no open request'}
        response = self.open_request.read()
        try:
            data = json.loads(response)
        except ValueError:
            quit("Cannot not parse response: %s\n" % response, JSON_ERROR)
        self.open_request = None
        return data


# End of Class TransmissionRequest


# Higher level of data exchange
class Transmission:
    STATUS_CHECK_WAIT = 1 << 0
    STATUS_CHECK      = 1 << 1
    STATUS_DOWNLOAD   = 1 << 2
    STATUS_SEED       = 1 << 3
    STATUS_STOPPED    = 1 << 4

    TAG_TORRENT_LIST    = 7
    TAG_TORRENT_DETAILS = 77
    TAG_SESSION_STATS   = 21
    TAG_SESSION_GET     = 22

    TRNSM_VERSION_MIN = '1.60'
    TRNSM_VERSION_MAX = '1.75'
    RPC_VERSION_MIN = 5
    RPC_VERSION_MAX = 6

    LIST_FIELDS = [ 'id', 'name', 'status', 'seeders', 'leechers', 'desiredAvailable',
                    'rateDownload', 'rateUpload', 'eta', 'uploadRatio',
                    'sizeWhenDone', 'haveValid', 'haveUnchecked', 'addedDate',
                    'uploadedEver', 'errorString', 'recheckProgress', 'swarmSpeed',
                    'peersKnown', 'peersConnected', 'uploadLimit', 'downloadLimit',
                    'uploadLimited', 'downloadLimited', 'bandwidthPriority']

    DETAIL_FIELDS = [ 'files', 'priorities', 'wanted', 'peers', 'trackers',
                      'activityDate', 'dateCreated', 'startDate', 'doneDate',
                      'totalSize', 'comment',
                      'announceURL', 'announceResponse', 'lastAnnounceTime',
                      'nextAnnounceTime', 'lastScrapeTime', 'nextScrapeTime',
                      'scrapeResponse', 'scrapeURL',
                      'hashString', 'timesCompleted', 'pieceCount', 'pieceSize', 'pieces',
                      'downloadedEver', 'corruptEver',
                      'peersFrom', 'peersSendingToUs', 'peersGettingFromUs' ] + LIST_FIELDS

    def __init__(self, host, port, username, password, debug = False):
        self.host = host
        self.port = port
        self._debug = debug

        if username and password:
            password_mgr = urllib2.HTTPPasswordMgrWithDefaultRealm()
            url = 'http://%s:%d/transmission/rpc' % (host, port)
            password_mgr.add_password(None, url, username, password)
            authhandler = urllib2.HTTPBasicAuthHandler(password_mgr)
            opener = urllib2.build_opener(authhandler)
            urllib2.install_opener(opener)

        # check rpc version
        request = TransmissionRequest(host, port, 'session-get', self.TAG_SESSION_GET)
        request.send_request()
        response = request.get_response()
        
        # rpc version too old?
        version_error = "Unsupported Transmission version: " + str(response['arguments']['version']) + \
            " -- RPC protocol version: " + str(response['arguments']['rpc-version']) + "\n"

        min_msg = "Please install Transmission version " + self.TRNSM_VERSION_MIN + " or higher.\n"
        try:
            if response['arguments']['rpc-version'] < self.RPC_VERSION_MIN:
                quit(version_error + min_msg)
        except KeyError:
            quit(version_error + min_msg)

        # rpc version too new?
        if response['arguments']['rpc-version'] > self.RPC_VERSION_MAX:
            quit(version_error + "Please install Transmission version " + self.TRNSM_VERSION_MAX + " or lower.\n")


        # set up request list
        self.requests = {'torrent-list':
                             TransmissionRequest(host, port, 'torrent-get', self.TAG_TORRENT_LIST, {'fields': self.LIST_FIELDS}),
                         'session-stats':
                             TransmissionRequest(host, port, 'session-stats', self.TAG_SESSION_STATS, 21),
                         'session-get':
                             TransmissionRequest(host, port, 'session-get', self.TAG_SESSION_GET),
                         'torrent-details':
                             TransmissionRequest(host, port)}

        self.torrent_cache = []
        self.status_cache  = dict()
        self.torrent_details_cache = dict()
        self.hosts_cache   = dict()
        self.geo_ips_cache = dict()
        if features['dns']:   self.resolver = adns.init()
        if features['geoip']: self.geo_ip = GeoIP.new(GeoIP.GEOIP_MEMORY_CACHE)

        # make sure there are no undefined values
        self.wait_for_torrentlist_update()

    def update(self, delay, tag_waiting_for=0):
        """Maintain up-to-date data."""

        tag_waiting_for_occurred = False

        for request in self.requests.values():
            if time.time() - request.last_update >= delay:
                request.last_update = time.time()
                response = request.get_response()

                if response['result'] == 'no open request':
                    request.send_request()

                elif response['result'] == 'success':
                    tag = self.parse_response(response)
                    if tag == tag_waiting_for:
                        tag_waiting_for_occurred = True

        if tag_waiting_for:
            return tag_waiting_for_occurred
        else:
            return None

    def parse_response(self, response):
        # response is a reply to torrent-get
        if response['tag'] == self.TAG_TORRENT_LIST or response['tag'] == self.TAG_TORRENT_DETAILS:
            for t in response['arguments']['torrents']:
                t['uploadRatio'] = round(float(t['uploadRatio']), 2)
                t['percent_done'] = self.percent(float(t['sizeWhenDone']),
                                            float(t['haveValid'] + t['haveUnchecked']))

            if response['tag'] == self.TAG_TORRENT_LIST:
                self.torrent_cache = response['arguments']['torrents']

            elif response['tag'] == self.TAG_TORRENT_DETAILS:
                self.torrent_details_cache = response['arguments']['torrents'][0]
                if features['dns'] or features['geoip']:
                    for peer in self.torrent_details_cache['peers']:
                        ip = peer['address']
                        if features['dns'] and not self.hosts_cache.has_key(ip):
                            self.hosts_cache[ip] = self.resolver.submit_reverse(ip, adns.rr.PTR)
                        if features['geoip'] and not self.geo_ips_cache.has_key(ip):
                            self.geo_ips_cache[ip] = self.geo_ip.country_code_by_addr(ip)
                            if self.geo_ips_cache[ip] == None:
                                self.geo_ips_cache[ip] = '?'

        elif response['tag'] == self.TAG_SESSION_STATS:
            self.status_cache.update(response['arguments'])

        elif response['tag'] == self.TAG_SESSION_GET:
            self.status_cache.update(response['arguments'])

        return response['tag']

    def get_global_stats(self):
        return self.status_cache

    def get_torrent_list(self, sort_orders = [], reverse=False):
        try:
            for sort_order in sort_orders:
                if isinstance(self.torrent_cache[0][sort_order], (str, unicode)):
                    self.torrent_cache.sort(key=lambda x: x[sort_order].lower(), reverse=reverse)
                else:
                    self.torrent_cache.sort(key=lambda x: x[sort_order], reverse=reverse)
        except IndexError:
            return []
        return self.torrent_cache

    def get_torrent_by_id(self, id):
        i = 0
        while self.torrent_cache[i]['id'] != id:  i += 1
        if self.torrent_cache[i]['id'] == id:
            return self.torrent_cache[i]
        else:
            return None

    def get_torrent_details(self):
        return self.torrent_details_cache
    def set_torrent_details_id(self, id):
        if id < 0:
            self.requests['torrent-details'] = TransmissionRequest(self.host, self.port)
        else:
            self.requests['torrent-details'].set_request_data('torrent-get', self.TAG_TORRENT_DETAILS,
                                                              {'ids':id, 'fields': self.DETAIL_FIELDS})
    def get_hosts(self):
        return self.hosts_cache

    def get_geo_ips(self):
        return self.geo_ips_cache

    def set_option(self, option_name, option_value):
        request = TransmissionRequest(self.host, self.port, 'session-set', 1, {option_name: option_value})
        request.send_request()
        self.wait_for_status_update()

    def set_rate_limit(self, direction, new_limit, torrent_id=-1):
        data = dict()
        if new_limit < 0:
            return
        elif new_limit == 0:
            new_limit     = None
            limit_enabled = False
        else:
            limit_enabled = True

        if torrent_id < 0:
            type = 'session-set'
            data['speed-limit-'+direction]            = new_limit
            data['speed-limit-'+direction+'-enabled'] = limit_enabled
        else:
            type = 'torrent-set'
            data['ids'] = [torrent_id]
            data[direction+'loadLimit']   = new_limit
            data[direction+'loadLimited'] = limit_enabled

        request = TransmissionRequest(self.host, self.port, type, 1, data)
        request.send_request()
        self.wait_for_torrentlist_update()


    def increase_bandwidth_priority(self, torrent_id):
        torrent = self.get_torrent_by_id(torrent_id)
        if torrent == None or torrent['bandwidthPriority'] >= 1:
            return False
        else:
            new_priority = torrent['bandwidthPriority'] + 1
            request = TransmissionRequest(self.host, self.port, 'torrent-set', 1,
                                          {'ids': [torrent_id], 'bandwidthPriority':new_priority})
            request.send_request()
            self.wait_for_torrentlist_update()

    def decrease_bandwidth_priority(self, torrent_id):
        torrent = self.get_torrent_by_id(torrent_id)
        if torrent == None or torrent['bandwidthPriority'] <= -1:
            return False
        else:
            new_priority = torrent['bandwidthPriority'] - 1
            request = TransmissionRequest(self.host, self.port, 'torrent-set', 1,
                                          {'ids': [torrent_id], 'bandwidthPriority':new_priority})
            request.send_request()
            self.wait_for_torrentlist_update()
        
    def stop_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-stop', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def start_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-start', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def verify_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-verify', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def remove_torrent(self, id):
        request = TransmissionRequest(self.host, self.port, 'torrent-remove', 1, {'ids': [id]})
        request.send_request()
        self.wait_for_torrentlist_update()

    def increase_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if not self.torrent_details_cache['wanted'][num]:
                ref_num = num
                break
            elif self.torrent_details_cache['priorities'][num] < \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if not self.torrent_details_cache['wanted'][ref_num]:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'high')

    def decrease_file_priority(self, file_nums):
        file_nums = list(file_nums)
        ref_num = file_nums[0]
        for num in file_nums:
            if self.torrent_details_cache['priorities'][num] > \
                    self.torrent_details_cache['priorities'][ref_num]:
                ref_num = num
        current_priority = self.torrent_details_cache['priorities'][ref_num]
        if current_priority >= 1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'normal')
        elif current_priority == 0:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'low')
        elif current_priority == -1:
            self.set_file_priority(self.torrent_details_cache['id'], file_nums, 'off')


    def set_file_priority(self, torrent_id, file_nums, priority):
        request_data = {'ids': [torrent_id]}
        if priority == 'off':
            request_data['files-unwanted'] = file_nums
        else:
            request_data['files-wanted'] = file_nums
            request_data['priority-' + priority] = file_nums
        request = TransmissionRequest(self.host, self.port, 'torrent-set', 1, request_data)
        request.send_request()
        self.wait_for_details_update()

    def get_file_priority(self, torrent_id, file_num):
        priority = self.torrent_details_cache['priorities'][file_num]
        if not self.torrent_details_cache['wanted'][file_num]: return 'off'
        elif priority <= -1: return 'low'
        elif priority == 0:  return 'normal'
        elif priority >= 1:  return 'high'
        return '?'


    def wait_for_torrentlist_update(self):
        self.wait_for_update(7)
    def wait_for_details_update(self):
        self.wait_for_update(77)
    def wait_for_status_update(self):
        self.wait_for_update(22)
    def wait_for_update(self, update_id):
        start = time.time()
        self.update(0) # send request
        while True:    # wait for response
            self.debug("still waiting for %d\n" % update_id)
            if self.update(0, update_id): break
            time.sleep(0.1)
        self.debug("delay was %dms\n\n" % ((time.time() - start) * 1000))

    def get_status(self, torrent):
        if torrent['status'] == Transmission.STATUS_CHECK_WAIT:
            status = 'will verify'
        elif torrent['status'] == Transmission.STATUS_CHECK:
            status = "verifying"
        elif torrent['status'] == Transmission.STATUS_SEED:
            status = 'seeding'
        elif torrent['status'] == Transmission.STATUS_DOWNLOAD:
            status = ('idle','downloading')[torrent['rateDownload'] > 0]
        elif torrent['status'] == Transmission.STATUS_STOPPED:
            status = 'paused'
        else:
            status = 'unknown state'
        return status

    def get_bandwidth_priority(self, torrent):
        if torrent['bandwidthPriority'] == -1:
            return '-'
        elif torrent['bandwidthPriority'] == 0:
            return ' '
        elif torrent['bandwidthPriority'] == 1:
            return '+'
        else:
            return '?'

    def debug(self,data):
        if self._debug:
            file = open("debug.log", 'a')
            file.write(data.encode('utf-8'))
            file.close
    
    @staticmethod
    def percent(full, part):
        try: percent = 100/(float(full) / float(part))
        except ZeroDivisionError: percent = 0.0
        return percent


# End of Class Transmission
