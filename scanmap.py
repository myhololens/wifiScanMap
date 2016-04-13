#!/usr/bin/env python

import subprocess
import threading
from threading import Lock
import time
from gps import *
import os
import sys
from os import path
import commands
import argparse
import re
import json
import sqlite3
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from SocketServer import ThreadingMixIn
from threading import Thread
import json
import urllib2
import urllib
import ssl
import shutil

import datetime

#meters
min_gpsd_accuracy = 30
default_airodump_age = 5

try:
  import RPi.GPIO as GPIO
  GPIO.setmode(GPIO.BCM) 
except:
  print "No gpio"

USE_SCAPY=False

if(USE_SCAPY):
  from scapy.all import *
  from scapy_ex import *
else:
  import csv
  
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--interface", help="wifi interface")    
    parser.add_argument("-s", "--sleep", help="wifi interface")  
    parser.add_argument("-d", "--database", help="wifi database")
    parser.add_argument('-w', '--www', help='www port')
    parser.add_argument('-a', '--accuracy', help='minimum accuracy')
    parser.add_argument('-u', '--synchro', help='synchro uri ie http://test.com:8686')
    parser.add_argument('-e', '--enable', action='store_true', help='enable db synchro through json')
    parser.add_argument('-m', '--monitor', action='store_true', help='use monitor mode instead of iwlist')
    parser.add_argument('-b', '--bssid', help='ignore bssid', action='append', nargs='*')
    return parser.parse_args()


class GpsPoller(threading.Thread):
  def __init__(self, gpsd, app):
    threading.Thread.__init__(self)
    self.gpsd = gpsd
    self.application = app
    self.date = False
    self.running = True #setting the thread running to true
    self.gpio = 17
    self.epx = 100
    self.epy = 100
    
    GPIO.setup(self.gpio, GPIO.OUT, initial=GPIO.LOW)

  def run(self):
    while self.running:
      self.gpsd.next() #this will continue to loop and grab EACH set of gpsd info to clear the buffer
      TIMEZ = 0 
      if self.gpsd.utc != None and self.gpsd.utc != '' and not self.date:
        self.date = True
        tzhour = int(self.gpsd.utc[11:13])+TIMEZ
        if (tzhour>23):
          tzhour = (int(self.gpsd.utc[11:13])+TIMEZ)-24
        gpstime = self.gpsd.utc[0:4] + self.gpsd.utc[5:7] + self.gpsd.utc[8:10] + ' ' + str(tzhour) + self.gpsd.utc[13:19]
        print 'Setting system time to GPS time...'
        os.system('sudo date --set="%s"' % gpstime)
      if self.has_fix:
        self.epx = self.gpsd.fix.epx
        self.epy = self.gpsd.fix.epy
        try:
          GPIO.output(self.gpio, GPIO.HIGH)
        except:
          pass
        q = 'insert into gps (latitude, longitude) values ("%s", "%s")'%(self.gpsd.fix.latitude, self.gpsd.fix.longitude)
        self.application.query(q)
      else:
        try:
          GPIO.output(self.gpio, GPIO.LOW)
        except:
          pass
  def getPrecision(self):
    return max(self.epx, self.epy)

  def has_fix(self, accurate = True):
    fix = self.gpsd.fix.mode > 1
    if( accurate ):
      fix = fix and self.epx < self.application.args.accuracy and self.epy < self.application.args.accuracy
    return fix
  
  def stop(self):
      self.running = False
    
class AirodumpPoller(threading.Thread):
  def __init__(self, app):
    threading.Thread.__init__(self)
    self.application = app
    self.lock = Lock()
    self.networks = []
    self.stations = []
    self.probes = []
    self.running = True #setting the thread running to true
    
    if self.application.args.sleep is not None:
      self.sleep = int(self.application.args.sleep)
    else:
      self.sleep = 1

  def date_from_str(self, _in):
    return datetime.datetime.strptime(_in.strip(), '%Y-%m-%d %H:%M:%S')
  
  def is_too_old(self, date, sleep):
    diff = datetime.datetime.now() - self.date_from_str(date)
    return diff.total_seconds() > sleep
  
  def run(self):
    while self.running:
            
      FNULL = open(os.devnull, 'w')
      prefix= 'wifi-dump'
      os.system("rm wifi-dump*")
      cmd = ['airodump-ng', '-w', prefix,  '--berlin', str(self.sleep), self.application.interface]
      process = subprocess.Popen(cmd, stderr=FNULL)
      time.sleep(10)
      #['BSSID', ' First time seen', ' Last time seen', ' channel', ' Speed', ' Privacy', ' Cipher', ' Authentication', ' Power', ' # beacons', ' # IV', ' LAN IP', ' ID-length', ' ESSID', ' Key']
      error_id = 0
      while self.running:
        fix = self.application.has_fix()
        lon, lat = self.application.getGPSData()
        wifis = []
        stations = []
        probes = []
        csv_path = "%s-01.csv"%prefix
        f = open(csv_path)
        for line in f:
          fields = line.split(', ')
          if len(fields) >= 13:
            if(fields[0] != 'BSSID'):
              n = {}
              try:
                if fix:
                  n["latitude"] = lat
                  n["longitude"] = lon
                n["bssid"] = fields[0]
                n["essid"] = fields[13].replace("\r\n", "")
                n["mode"] = 'Master'
                n["channel"] = fields[3]
                n["frequency"] = -1
                n["manufacturer"] = self.application.getManufacturer(n["bssid"])
                n["signal"] = float(fields[8])
                
                if(n["signal"] >= -1):
                  n["signal"] = -100
                
                n["encryption"] = fields[5].strip() != "OPN"
                if not self.is_too_old(fields[2], default_airodump_age):
                  if n["bssid"] not in self.application.ignore_bssid:
                    wifis.append(n)
              except Exception as e:
                self.application.log("wifi", 'parse fail')
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                print(exc_type, fname, exc_tb.tb_lineno)
                self.application.log('airodump' , fields)
                self.application.log('airodump' , n)
                shutil.copyfile(csv_path, "/tmp/wifimap-%s.csv"%error_id)
                error_id += 1
          elif len(fields) == 7 or len(fields) == 6:
            try:
              if(fields[0] != 'Station MAC'):
                s = {}
                s['bssid'] = fields[0]
                s['last_seen'] = fields[2]
                s['signal'] = float(fields[3])
                s["manufacturer"] = self.application.getManufacturer(s["bssid"])
                if fix:
                  s['latitude'] = lat
                  s['longitude'] = lon
                
                if not self.is_too_old(fields[2], default_airodump_age):
                  stations.append(s)
                
                if len(fields) == 7:
                  for r in fields[6].split(','):
                    p = {}
                    p['bssid'] = fields[0]
                    p['signal'] = s['signal']
                    p['manufacturer'] = s["manufacturer"]
                    p['essid'] = r.replace("\r\n", "")
                    if p['essid'] != "":
                      if not self.is_too_old(s['last_seen'], default_airodump_age):
                        probes.append(p)
            except:
              self.application.log("wifi", 'station parse fail')
        f.close()
        with self.lock:
          self.networks = wifis
          self.stations = stations
          self.probes = probes
        time.sleep(self.sleep/2)
        
  def getNetworks(self):
    with self.lock:
      return self.networks
          
  def stop(self):
      self.running = False

class Synchronizer(threading.Thread):
  def __init__(self, application, uri):
    self.application = application
    threading.Thread.__init__(self)
    self.running = True #setting the thread running to true
    self.base = uri

  def run(self):
    time.sleep(5)
    while self.running:
      
      try:
        context = ssl._create_unverified_context()
        raw = urllib2.urlopen("%s/status.json"%self.base, context=context)
        date = json.loads(raw.read())["sync"]
        if date is not None:
          date = date[0].split('.')[0]
        else:
          date = '1980-01-01 00:00:00'
        
        ap = self.application.getAll(date)['networks']
        p = self.application.getAllProbes()['probes']
        s = self.application.getAllStations('like "%%" or date > "%s"'%date)
        data = {
          'ap':ap,
          'probes': p,
          'stations': s
                }
        
        req = urllib2.Request('%s/upload.json'%self.base)
        req.add_header('Content-Type', 'application/json')
        response = urllib2.urlopen(req, json.dumps(data), context=context)
        print "sync"
      except:
        print "Sync unavailable"
      time.sleep(60)

  def stop(self):
      self.running = False
      
class WebuiHTTPHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
      pass
    
    def _parse_url(self):
        # parse URL
        path = self.path.strip('/')
        sp = path.split('?')
        if len(sp) == 2:
            path, params = sp
        else:
            path, = sp
            params = None
        args = path.split('/')

        return path,params,args
    
    def _get_index(self):
      self.send_response(200)
      self.send_header('Content-type','text/html')
      self.end_headers()
      networks = self.server.app.getAll()
      html = '''
      <script type="text/javascript" src="js/OpenLayers.js"></script>
      <script type="text/javascript" src="js/OpenStreetMap.js"></script>
      <script src="js/jquery-1.11.2.min.js"></script>
      <script>
      var current_gps_position;
      var current_wifi_position;
      var markers;
          function update(){
          $.getJSON('/status.json').done( function(data){
              if(data['position']['gps']['fix'])
              {
                  var lonLat = new OpenLayers.LonLat( data['position']['gps']['longitude'] ,data['position']['gps']['latitude']).transform( new OpenLayers.Projection("EPSG:4326"), map.getProjectionObject() );
                  current_gps_position.move(lonLat);
              }
              if(data['position']['wifi']['fix'])
              {
                  var lonLat = new OpenLayers.LonLat( data['position']['wifi']['longitude'] ,data['position']['wifi']['latitude']).transform( new OpenLayers.Projection("EPSG:4326"), map.getProjectionObject() );
                  current_wifi_position.move(lonLat);
              }
              
          }) .fail(function(d, textStatus, error) {
      console.error("getJSON failed, status: " + textStatus + ", error: "+error)
});
          setTimeout(update,1000);
          }
      
          var map;
      
          var fromProjection = new OpenLayers.Projection("EPSG:4326");   // Transform from WGS 1984
          var toProjection   = new OpenLayers.Projection("EPSG:900913"); // to Spherical Mercator Projection
  
          function init(){
              map = new OpenLayers.Map('map',
                      { maxExtent: new OpenLayers.Bounds(-20037508.34,-20037508.34,20037508.34,20037508.34),
                      numZoomLevels: 21,
                      maxResolution: 156543.0399,
                      units: 'm'
                      });
              map.addLayer(new OpenLayers.Layer.OSM());
            
              
              position_layer = new OpenLayers.Layer.Vector("position");
              map.addLayer(position_layer);
              
              markers = new OpenLayers.Layer.Markers( "Markers" );
              map.addLayer(markers);
              '''
      if networks['center'] is not None:
        html+='''
        var lonLat = new OpenLayers.LonLat('''+str(networks['center'][1])+", "+str(networks['center'][0])+''').transform( fromProjection, toProjection);
        if (!map.getCenter()) map.setCenter (lonLat, 16);
        '''
      lastLat = None
      lastLon = None
      count = 0
      def generate(networks_same_position):
        count = len(networks_same_position)
        if count == 0:
          return ''
        html = ''
        names = '<ul>'
        open_icon=''
        for i in networks_same_position:
          key = ''
          if not i[2]:
            open_icon='-open'
          else:
            key = '<img src=\\"locked.png\\">'
          manufacturer = self.server.app.getManufacturer(i[0])
          ssid = unicode(i[1]).encode('utf8')
          names = "%s<li>%s %s %s</li>"%(names,key, ssid, manufacturer)
        name = "%s</ul>"%names
        icon = "marker%s.png"%open_icon
        if count >= 2:
          icon ="marker-few%s.png"%open_icon
        if count >= 4:
          icon ="marker-many%s.png"%open_icon
        html+= '''
      setMarker(markers, '''+str(lat)+''', '''+str(lon)+''', "'''+names+'''", "'''+icon+'''");'''
        return html
      
      
      networks_same_position = []
      for n in networks["networks"]:
          lat = n[5]
          lon = n[4]
          name = n[1]
          if lastLat == None:
            lastLat = lat
            lastLon = lon
          if lat == lastLat and lon == lastLon:
            networks_same_position.append(n)
          else:
            html += generate(networks_same_position)
            networks_same_position = []
            networks_same_position.append(n)
            
          lastLat = lat
          lastLon = lon
      html += generate(networks_same_position)
      html +='''
            current_gps_position = new OpenLayers.Feature.Vector(
                  new OpenLayers.Geometry.Point(0,0),
                  {}, {
                  fillColor : 'red',
                  fillOpacity : 1,                    
                  strokeColor : "#ffffff",
                  strokeOpacity : 1,
                  strokeWidth : 1,
                  pointRadius : 8
                  }
              );
              
              current_wifi_position = new OpenLayers.Feature.Vector(
                  new OpenLayers.Geometry.Point(0,0),
                  {}, {
                  fillColor : 'green',
                  fillOpacity : 1,                    
                  strokeColor : "#ffffff",
                  strokeOpacity : 1,
                  strokeWidth : 1,
                  pointRadius : 8
                  }
              );

              position_layer.addFeatures(current_gps_position);
              position_layer.addFeatures(current_wifi_position);
      
              setTimeout(update,1000);
          }
          
          function setMarker(markers, lat, lon, contentHTML, icon){
              var lonLatMarker = new OpenLayers.LonLat(lon, lat).transform( fromProjection, toProjection);
              var feature = new OpenLayers.Feature(markers, lonLatMarker);
              feature.closeBox = true;
              feature.popupClass = OpenLayers.Class(OpenLayers.Popup.FramedCloud, {minSize: new OpenLayers.Size(300, 180) } );
              feature.data.popupContentHTML = contentHTML;
              feature.data.overflow = "auto";
              
              if(icon != "")
              {
                  var icon = new OpenLayers.Icon(icon,new OpenLayers.Size(20, 50), new OpenLayers.Pixel(-10,-50));
                  var marker = new OpenLayers.Marker(lonLatMarker, icon);
                  marker.feature = feature;
              }

              if(contentHTML != "")
              {
                  var markerClick = function(evt) {
                          if (this.popup == null) {
                                  this.popup = this.createPopup(this.closeBox);
                                  map.addPopup(this.popup);
                                  this.popup.show();
                          } else {
                                  this.popup.toggle();
                          }
                          OpenLayers.Event.stop(evt);
                  };
                  marker.events.register("mousedown", feature, markerClick);
              }   

              markers.addMarker(marker);
      }
      </script>
      '''
      
      html += '''<body onload="init()">
      <div id="map"></map>
      </body>
      '''
      
      self.wfile.write(html)
    
    def _get_status(self):
      gps_status = self.server.app.has_fix(True)
      wifi_status = self.server.app.wifiPosition is not None
      status = {
      'wifi': {'updated':self.server.app.last_updated},
      'position': {
        'gps':{
          'fix':(gps_status)
          },
        'wifi':{
          'fix':(wifi_status)
          }
        }
      }
      
      status["stat"] = self.server.app.getStat()
      status["current"] = self.server.app.getCurrent()
      
      if gps_status:
          status['position']['gps']['latitude'] = self.server.app.session.fix.latitude
          status['position']['gps']['longitude'] = self.server.app.session.fix.longitude
          status['position']['gps']['accuracy'] = self.server.app.gpspoller.getPrecision()
      
      status['sync'] = self.server.app.getLastUpdate()
      
      wifiPos = self.server.app.wifiPosition
      if wifiPos is not None:
        status['position']['wifi']['latitude'] = wifiPos[0]
        status['position']['wifi']['longitude'] = wifiPos[1]
      
      self.send_response(200)
      self.send_header('Access-Control-Allow-Origin','*')
      self.end_headers()
      # push data
      self.wfile.write(json.dumps(status))
    
    def _get_file(self, path):
      _path = os.path.join(self.server.www_directory,path)
      if os.path.exists(_path):
          try:
          # open asked file
              data = open(_path,'r').read()

              # send HTTP OK
              self.send_response(200)
              self.end_headers()

              # push data
              self.wfile.write(data)
          except IOError as e:
                self.send_500(str(e))
      
    def _get_kml(self):
      try:
        self.send_response(200)
        self.send_header('Content-type','text/html')
        self.end_headers()
        import simplekml
        kml = simplekml.Kml()
        networks = self.server.app.getAll()
        
        for n in networks["networks"]:
          lat = n[5]
          lon = n[4]
          name = n[1]
          kml.newpoint(name=name, coords=[(lon,lat)])
          kml_str = unicode(kml.kml()).encode('utf8')
        self.wfile.write(kml_str)
      except:
        self.send_response(500)
    
    def _get_wifis(self):
      self.send_response(200)
      self.send_header('Content-type','application/json')
      self.send_header('Access-Control-Allow-Origin','*')
      self.end_headers()
      networks = self.server.app.getAll()
      data = []
      for n in networks["networks"]:
        d = {}
        d["latitude"] = n[5]
        d["longitude"] = n[4]
        d["essid"] = n[1]
        d["bssid"] = n[0]
        d["encryption"] = n[2]
        data.append(d)
      
      self.wfile.write(json.dumps(data))
    
    def _get_stations(self, search = None):
      self.send_response(200)
      self.send_header('Content-type','application/json')
      self.send_header('Access-Control-Allow-Origin','*')
      self.end_headers()
      data = self.server.app.getAllStations(search)
      self.wfile.write(json.dumps(data))
    
    def _get_probes(self):
      self.send_response(200)
      self.send_header('Content-type','application/json')
      self.send_header('Access-Control-Allow-Origin','*')
      self.end_headers()
      probes = self.server.app.getAllProbes(True)
      data = []
      for n in probes["probes"]:
        s = {}
        s["essid"] = n[0]
        s["count"] = n[1]
        data.append(s)
      
      self.wfile.write(json.dumps(data))
    
    def _get_csv(self):
      #try:
      self.send_response(200)
      self.send_header('Content-type','text/html')
      self.end_headers()
      networks = self.server.app.getAll()
      csv="SSID;BSSID;ENCRYPTION;LATITUDE;LONGITUDE;\r\n"
      for n in networks["networks"]:
        lat = n[5]
        lon = n[4]
        ssid = n[1]
        bssid = n[0]
        encryption = n[2]
        csv += '"%s"; "%s"; "%s"; %s; %s\r\n'%(ssid, bssid, encryption, lat, lon)
      csv = unicode(csv).encode('utf8')
      self.wfile.write(csv)
      #except:
        #self.send_response(500)
    
    def setParam(self, key,value):
      if key == 'minAccuracy':
        self.send_response(200)
        self.send_header('Content-type','text/html')
        self.end_headers()
        self.server.app.args.accuracy = float(value)
        self.wfile.write(json.dumps('ok'))
    
    def do_POST(self):
        path,params,args = self._parse_url()
        if ('..' in args) or ('.' in args):
            self.send_400()
            return
        if len(args) == 1 and args[0] == 'upload.json':
          if(not self.server.app.args.enable):
            self.send_response(403)
            return
          self.send_response(200)
          self.send_header('Content-type','text/html')
          self.end_headers()
          length = int(self.headers['Content-Length'])
          post = self.rfile.read(length)
          post = post.decode('string-escape').strip('"')
          data = json.loads(post,strict=False)
          for n in data['ap']:
            network = {}
            network['bssid'] = n[0]
            network['essid'] = n[1]
            network['encryption'] = n[2]
            network['signal'] = n[3]
            network['longitude'] = n[4]
            network['latitude'] = n[5]
            network['frequency'] = n[6]
            network['channel'] = n[7]
            network['mode'] = n[8]
            network['date'] = n[9]
            self.server.app.update(network)
          
          for bssid in data['stations']:
            for n in data['stations'][bssid]["points"]:
              p = n
              p['bssid'] = bssid
              self.server.app.update_station(p)
          
          for probe in data['probes']:
            p = {}
            p['bssid'] = probe[0]
            p['essid'] = probe[1]
            self.server.app.update_probe(p)
          
          self.wfile.write(json.dumps('ok'))
    
    def do_GET(self):
        path,params,args = self._parse_url()
        if ('..' in args) or ('.' in args):
            self.send_400()
            return
        if len(args) == 1 and args[0] == '':
            return self._get_index()
        elif len(args) == 1 and args[0] == 'status.json':
            return self._get_status()
        elif len(args) == 1 and args[0] == 'set':
          key = params.split('=')[0]
          value = params.split('=')[1]
          return self.setParam(key,value)
        elif len(args) == 1 and args[0] == 'kml':
            return self._get_kml()
        elif len(args) == 1 and args[0] == 'csv':
            return self._get_csv()
        elif len(args) == 1 and args[0] == 'wifis.json':
            return self._get_wifis()
        elif len(args) == 1 and args[0] == 'stations.json':
            if params is not None:
              params = params.split('search=')[1]
            return self._get_stations(params)
        elif len(args) == 1 and args[0] == 'probes.json':
            return self._get_probes()
        elif len(args) == 1 and args[0] == 'current.json':
            return self._get_current()
        else:
            return self._get_file(path)
      
class WebuiHTTPServer(ThreadingMixIn, HTTPServer, Thread):
  def __init__(self, server_address, app, RequestHandlerClass, bind_and_activate=True):
    HTTPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate)
    threading.Thread.__init__(self)
    self.app = app
    self.www_directory = "www/"
    self.stopped = False
    
  def stop(self):
    self.stopped = True
    
  def run(self):
      while not self.stopped:
          self.handle_request()
      
class Application (threading.Thread):
    def __init__(self, args):
        threading.Thread.__init__(self)
        self.args = args
        self.manufacturers_db = '/usr/share/wireshark/manuf'
        self.manufacturers = {}
        self.lock = Lock()
        self.stopped = False
        self.session = gps(mode=WATCH_ENABLE)
        self.ignore_bssid = []
        self.last_updated = 0
        self.network_count = 0
        self.interface = ''
        self.airodump = None
        self.wifiPosition = None
        
        if(self.args.accuracy is None):
          self.args.accuracy = min_gpsd_accuracy
        
        if(self.args.database is not None):
            db = self.args.database
        else:
            db = "./wifimap.db"
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.query_db = self.db.cursor()
        
        try:
            self.query('''select * from wifis''')
        except:
            self.createDatabase()
        
        self.gpspoller = GpsPoller(self.session, self)
        self.gpspoller.start()
        
        for b in self.getConfig('bssid').split(','):
          self.ignore_bssid.append(b)
        
        if args.bssid is not None:
          for b in args.bssid:
            self.ignore_bssid.append(b)
        
        if not args.enable:
          args.enable = self.getConfig('enable') == 'true'
        
        if args.synchro is None:
          if self.getConfig('synchro') != '':
            args.synchro = self.getConfig('synchro')
        
        if args.synchro is not None:
          self.synchronizer = Synchronizer(self, args.synchro)
          self.synchronizer.start()
        
        try:
          if self.args.interface is not None:
              self.interface = self.args.interface
          else:
              self.interface = self.getWirelessInterfacesList()[0]
          self.ignore_bssid.append(self.getMacFromIface(self.interface))
        except:
          self.log("App", "No wifi interface")
        
        if self.args.monitor:
          if self.interface != 'mon0':
            cmd = ['airmon-ng', 'start' ,self.interface]
            p = subprocess.Popen(cmd)
            p.wait()
          self.interface = 'mon0'
                
        
        print self.getConfig('www')
        if self.args.www is not None:
            port = int(self.args.www)
        else:
          if self.getConfig('www') != '':
            port = int(self.getConfig('www'))
          else:
            port = 8686
            
        self.loadManufacturers()
        self.httpd = WebuiHTTPServer(("", port),self, WebuiHTTPHandler)
        self.httpd.start()
    
    def query(self, query):
      with self.lock:
        self.query_db.execute(query)
    
    def fetchone(self, query):
      with self.lock:
        self.query_db.execute(query)
        return self.query_db.fetchone()
    
    def fetchall(self, query):
      with self.lock:
        self.query_db.execute(query)
        return self.query_db.fetchall()
    
    def getConfig(self, _key):
      try:
        q = '''select value from config where key == "%s"'''%_key
        res = self.query.fetchone(q)
        return res[0]
      except:
        return ''
    
    def packet_handler(self, pkt):
      if pkt.haslayer(Dot11):
        if pkt.type == 0 and pkt.subtype == 8:
          rssi =0
          print "==> %s %s %s"%(rssi, pkt.addr2, pkt.info)
          pkt.show()
    
    def getStat(self):
      stat = {}
      q = '''select count(*) from wifis where encryption == 0'''
      stat['open_count'] = self.fetchone(q)[0]
      
      q = '''select count(*) from wifis'''
      stat['total'] = self.fetchone(q)[0]
      
      q = '''select essid, count(*) as nb from wifis group by essid order by nb desc limit 15'''
      stat['best'] = self.fetchall(q)
      return stat
    
    def getLastUpdate(self):
        q = '''select date from wifis order by date desc limit 1'''
        return self.fetchone(q)
    
    def getWifisFromEssid(self, essid):
      where = ''
      if isinstance(essid, collections.Sequence):
        where = 'essid in ("%s")'%','.join(essid)
      else:
        where = 'essid="%s"'%essid 
      
      q='select * from wifis where %s'%where
      return self.fetchall(q)
      
    
    def getStationsPerDay(self, limit = 0):
      limit_str = ''
      if limit_str != 0:
        limit_str = 'LIMIT %s'%limit
      q='''select bssid, date(date), count(distinct date(date)) from stations group by bssid order by count(distinct date(date)) DESC, date %s'''%limit
      return self.fetchall(q)
    
    def getAllStations(self, search = None):
      stations = {}
      searchs = []
      search_where = ''
      if search is not None:
        if len(search.split(',')) != 1:
          for s in search.split(','):
            searchs.append( '"%s"'%s.strip())
          search_where = 'where bssid in (%s)'%','.join(searchs)
        else:
          search_where = 'where bssid %s'%urllib.unquote(search)
      q = "select distinct (bssid) from stations %s"%search_where
      for s in self.fetchall(q):
        bssid = s[0]
        if not stations.has_key(bssid):
          stations[bssid] = {}
          stations[bssid]["manufacturer"] = self.getManufacturer(bssid)
          stations[bssid]["points"] = []
                    
        q = 'select * from stations where bssid="%s"'%bssid
        for r in self.fetchall(q):
          station = {}
          station["latitude"] = r[2]
          station["longitude"] = r[3]
          station["signal"] = r[4]
          station["date"] = r[5]
          stations[bssid]["points"].append(station)
      return stations
    
    def getAllProbes(self, distinct = False):
      probes = {}
      if not distinct:
        q = 'select * from probes order by essid'
      else:
        q = 'select essid, count(*) from probes group by essid order by count(*) desc'
      probes["probes"] = self.fetchall(q)
      return probes
    
    def getAll(self, date = None):
        wifis = {}
        date_where = ''
        if date is not None:
          date_where = 'where date > "%s"'%date
        q = 'select * from wifis %s order by latitude, longitude'%date_where
        wifis["networks"] = self.fetchall(q)
        
        q = 'select avg(latitude), avg(longitude) from wifis %s group by date order by date desc limit 1'%date_where
        wifis["center"] = self.fetchone(q)
        wifis["stat"] = self.getStat()
        return wifis
    
    def getLast(self):
        wifis = {}
        q = '''select * from wifis order by date desc, latitude, longitude limit 10'''
        wifis["networks"] = self.fetchall(q)
        wifis["stat"] = self.getStat()
        return wifis
    
    def getCurrent(self):
      wifis = self.scanForWifiNetworks()
      probes = []
      stations = []
      
      if self.args.monitor and not USE_SCAPY:
        probes = self.airodump.probes
        stations = self.airodump.stations
      data = {}
      data['wifis'] = wifis
      data['probes'] = probes
      data['stations'] = stations
      return data
    
    def createDatabase(self):
        print "initiallize db"
        self.query('''CREATE TABLE wifis
            (bssid text, essid text, encryption bool, signal real, longitude real, latitude real, frequency real, channel int, mode text, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        self.query('''CREATE TABLE config
            (key text, value text)''')
        self.query('''CREATE TABLE gps
            (latitude real, longitude real, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        self.query('''CREATE TABLE stations
            (id integer primary key, bssid  text, latitude real, longitude real, signal real, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        self.query('''CREATE TABLE probes (bssid  text, essid text)''')
    
    def log(self, name, value):
        print "%s   %s : %s"%(datetime.datetime.now(), name, value)
    
    def stop(self):
        self.stopped = True
        self.gpspoller.stop()
        self.gpspoller.join()
        self.synchronizer.stop()
        self.synchronizer.join()
        self.httpd.stop()
        self.httpd.join()
        self.airodump.stop()
        self.airodump.join()
    
    def has_fix(self, accurate = True):
      return self.gpspoller.has_fix(accurate)
    
    def run(self):
        if self.interface == '':
          self.log("wifi", "no interface")
          while not self.stopped:
            time.sleep(1)
          return
        if self.args.monitor:
          if USE_SCAPY:
            sniff(iface=self.interface, prn = self.packet_handler)
          else:
            self.airodump = AirodumpPoller(self)
            self.airodump.start()
        
        while not self.stopped:
          try:
            wifis = self.scanForWifiNetworks()
            updated = 0
            for w in wifis:
              try:
                if self.update(w):
                    updated += 1
              except:
                self.log("wifi", "insert fails")
                print w
                
            if updated != 0:
                self.log("updated wifi", updated)
            self.last_updated = updated
            self.network_count = len(wifis)
            self.wifiPosition = self.getWifiPosition(wifis)
            
            if self.args.monitor and not USE_SCAPY:
              try:
                updated = 0
                for p in self.airodump.probes:
                  if self.update_probe(p):
                    updated += 1
                  if updated != 0:
                    self.log("updated probes", updated)
              except:
                self.log("wifi", "probes insert fails")
              
              try:
                updated = 0
                for s in self.airodump.stations:
                  if self.update_station(s):
                    updated += 1
                
                if updated != 0:
                    self.log("updated stations", updated)
              except:
                self.log("wifi", "stations insert fails")
            
            self.db.commit()
          except Exception as e:
            self.log("wifi", 'fail')
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
          if self.args.sleep is not None:
              sleep = int(self.args.sleep)
          else:
              sleep = 1
          
          time.sleep(sleep)
      
    def update(self, wifi):
        if not wifi.has_key('latitude'):
          return False
        if math.isnan(wifi["longitude"]):
            return False
                
        q = '''select * from wifis where bssid="%s" and essid="%s"'''%(wifi["bssid"], wifi["essid"])
        res = self.fetchone(q)
        if res is None:
            q = 'insert into wifis (bssid, essid, encryption, signal, longitude, latitude, frequency, channel, mode, date) values ("%s", "%s", %s, %s, %s, %s, %s, %s, "%s", CURRENT_TIMESTAMP )'%(wifi["bssid"], wifi["essid"], int(wifi["encryption"]), wifi["signal"], wifi["longitude"], wifi["latitude"], wifi["frequency"], wifi["channel"], wifi["mode"])
            try:
              self.query(q)
              return True
            except:
              print "sqlError: %s"%q
              return False
        else:
            try:
              signal = res[3]
              q = 'update wifis set bssid="%s", essid="%s", encryption=%s, signal=%s, longitude=%s, latitude=%s, frequency=%s, channel=%s, mode="%s", date=CURRENT_TIMESTAMP where bssid="%s" and essid="%s"'%(wifi["bssid"], wifi["essid"], int(wifi["encryption"]), wifi["signal"], wifi["longitude"], wifi["latitude"], wifi["frequency"], wifi["channel"], wifi["mode"], wifi["bssid"], wifi["essid"])
              if wifi["signal"] < signal:
                  self.query(q)
                  return True
            except:
              print "sqlError: %s"%q
        return False
    
    def update_probe(self, probe):
      q = '''select * from probes where bssid="%s" and essid="%s"'''%(probe["bssid"], probe["essid"])
      res = self.fetchone(q)
      if res is None:
        q = '''insert into probes (bssid, essid) values ("%s", "%s")'''%(probe["bssid"], probe["essid"])
        self.query(q)
        return True
      return False

    def update_station(self, station):
      if not station.has_key('latitude'):
        return False
      q = '''select * from stations where bssid="%s" and latitude="%s" and longitude="%s"'''%(station["bssid"], station["latitude"], station["longitude"])
      res = self.fetchone(q)
      if res is None:
        q = '''insert into stations (id, bssid, latitude, longitude, signal) values (NULL, "%s", "%s", "%s", "%s")'''%(station["bssid"], station["latitude"], station["longitude"], station["signal"])
        self.query(q)
        self.log("station", "last_seen %s"%station["last_seen"])
        return True
      return False
    
    def loadManufacturers(self):
      try:
        manuf = open(self.manufacturers_db,'r').read()
        res = re.findall("(..:..:..)\s(.*)\s#\s(.*)", manuf)
        if res is not None:
          for m in res:
            self.manufacturers[m[0]] = m[1]
      except:
        pass
      return ''
    
    
    def getManufacturer(self,_bssid):
      try:
        # keep only 3 first bytes
        signature = ':'.join(_bssid.split(":")[:3])
        return self.manufacturers[signature.upper()]
      except:
        pass
      return ''
    
    def scanForWifiNetworks(self):
        if self.args.monitor:
          return self.airodump.getNetworks()
        else:
          networkInterface = self.interface
          output = ""
          if(networkInterface!=None):		
              command = ["iwlist", networkInterface, "scanning"]
              process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
              process.wait()
              (stdoutdata, stderrdata) = process.communicate();
              output =  stdoutdata
              return self.parseIwlistOutput(output)
    
    def parseIwlistOutput(self, data):
        networks = {}
        res = re.findall("Address: (.*)", data)
        if res is not None:
            networks["bssid"] = res
        
        res = re.findall("ESSID:\"(.*)\"", data)
        if res is not None:
            networks["essid"] = res
    
        res = re.findall("Mode:(.*)", data)
        if res is not None:
            networks["mode"] = res
            
        res = re.findall("Channel:(\d*)", data)
        if res is not None:
            networks["channel"] = res
            
        res = re.findall("Frequency:(.*) GHz", data)
        if res is not None:
            networks["frequency"] = res
    
        res = re.findall("Signal level=(.*) dBm", data)
        if res is not None:
            networks["signal"] = res
        
        res = re.findall("Encryption key:(.*)", data)
        if res is not None:
            networks["encryption"] = res
            
        lon, lat = self.getGPSData()
        wifis = []
        
        for i in range(0,len(networks["essid"])):
            n = {}
            if self.has_fix():
              if lat !=0 and lon != 0:
                n["latitude"] = lat
                n["longitude"] = lon
            n["bssid"] = networks["bssid"][i]
            n["essid"] = networks["essid"][i]
            n["mode"] = networks["mode"][i]
            n["channel"] = networks["channel"][i]
            n["frequency"] = float(networks["frequency"][i])
            n["signal"] = float(networks["signal"][i])
            n["encryption"] = networks["encryption"][i] == "on"
            if n["bssid"] not in self.ignore_bssid:
                wifis.append(n)
        return wifis
        
            
    def getWifiPosition(self, wifis):
      bssid = []
      if len(wifis) == 0:
        return None
      for n in wifis:
        bssid.append("\"%s\""%n["bssid"])
      q = "select avg(latitude), avg(longitude) from wifis where bssid in ( %s )"%(','.join(bssid))
      res = self.fetchone(q)
      if res is not None:
        if res[0] is None:
          return None
        return (res[0], res[1])
           
    def getGPSData(self):
        longitude = self.session.fix.longitude
        latitude = self.session.fix.latitude
        return (longitude, latitude)
            
    def getWirelessInterfacesList(self):
        networkInterfaces=[]		
        command = ["iwconfig"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.wait()
        (stdoutdata, stderrdata) = process.communicate();
        output = stdoutdata
        lines = output.splitlines()
        for line in lines:
                if(line.find("IEEE 802.11")!=-1):
                        networkInterfaces.append(line.split()[0])
        return networkInterfaces
      
    def getMacFromIface(self, _iface):
      path = "/sys/class/net/%s/address"%_iface
      data = open(path,'r').read()
      data = data[0:-1] # remove EOL
      return data

def main(args):
    app = Application(args)
    app.start()
    try:
        while True:
          time.sleep(1)
    except KeyboardInterrupt:
        print "Exiting..."
        app.stop()
    print "stopped"
        


main(parse_args())
