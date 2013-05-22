#!/usr/bin/env python

import argparse
import csv
import fnmatch
import glob
import json
import logging
import matplotlib.nxutils as nx
import multiprocessing
import numpy
import os
import pp
import signal
import sqlite3
import stat
import string
import subprocess
import sys
import tarfile
import time
import traceback
import Queue
import fiona

from fiona import collection
from collections import defaultdict
from multiprocessing import Lock
from string import Template

logger = logging.getLogger (__name__)

class DataImporter (object):
    ''' Load CSV data into SQLite3 accessible memory to filter. '''

    def __init__ (self, query, columns, database, output_directory = "output"):
        ''' Initialize connection. '''
        self.con = sqlite3.connect (database)
        self.cur = self.con.cursor ()
        self.columns = columns
        insert_columns = ", ".join (self.columns)
        value_slots = ",".join ([ "?" for x in self.columns ])
        self.insert_statement = "INSERT INTO simulation (%s) VALUES (%s);" % (insert_columns, value_slots)
        self.output_directory = output_directory
        self.query = query

    def create_table (self):
        ''' Build the table from a dynamically constructed column list. '''
        column_ddl = map (lambda x : "%s varchar(10)" % x, self.columns)
        column_ddl = ", ".join (column_ddl)
        statement = "CREATE TABLE simulation (%s);" % column_ddl
        logger.debug ("SQL: %s", statement)
        self.cur.execute (statement)

    def drop_table (self):
        try:
            self.cur.execute ('DROP TABLE simulation')
        except sqlite3.OperationalError:
            pass

    def load_data (self, input_file, columns, delimiter = '\t'):
        ''' Clean the database, create the table and import data from CSV. '''
        self.drop_table ()
        self.create_table ()
        with open (input_file, 'rbU') as stream:
            data = csv.reader (stream, delimiter = delimiter)
            for index, row in enumerate (data):
                try:
                    self.cur.executemany (self.insert_statement, (row,))
                except sqlite3.ProgrammingError:
                    pass
        self.con.commit ()

    def get_counts (self, file_name, fips_county_map):
        ''' Apply the filter to get location data. '''
        timeslice_id_index = file_name.rindex ('.') + 1
        assert timeslice_id_index < len (file_name), "Problem parsing filename: %s" % file_name
        timeslice_id = file_name [timeslice_id_index:]

        ''' File name form: /population/population.tsv.0045.intervention.0004 '''
        scenario_dir = file_name.split ('/')[-2].split ('.')
        scenario = '.'.join ( [ scenario_dir [-2], scenario_dir [-1] ] )
        coordinates = []
        for metric in self.query:
            query = self.query [metric]
            print "Query metric: %s" % metric
            print fips_county_map
            for idx, county_code in enumerate (fips_county_map):
                formatted_query = query.format (county_code)
                self.cur.execute (formatted_query)
                count = self.cur.fetchone ()

                if count[0] > 0:
                    rows = self.cur.execute ("select count(*) from simulation").fetchall ()
                    for row in rows:
                        logger.debug ("row => %s", row)

                    hits = self.cur.execute ("select * from simulation where cancer_free_years > 0.0 and stcotrbg like '37049%'").fetchall ()
                    for hit in hits:
                        logger.debug ("hit => %s", hit)


                    logger.debug ("Querying: %s => %s", formatted_query, count)
                coordinates.append ( [ metric, scenario, timeslice_id, county_code, idx, count ] )
        return coordinates
    
    def data_import (self, file_name, output_file_path, fips_county_map):
        ''' Create the output directory, import input data and select and write output data. '''
        fs_lock.acquire ()
        if not os.path.exists (self.output_directory):
            os.makedirs (self.output_directory)
        fs_lock.release ()

        self.load_data (file_name, self.columns)
        counts = self.get_counts (file_name, fips_county_map)

        with open (output_file_path, 'w') as stream:
            for tup in counts:
                text = "%s %s %s %s %s %s\n" % (tup[0], tup[1], tup[2], tup[3], tup[4], tup[5][0])
                stream.write (text)

def select_worker (query, database, output_dir, fips_county_map, work_Q):
    ''' Determine column names, create a data cruncher and process the input file. '''
    while True:
        try:
            snapshot_file = work_Q.get (timeout = 2)
            logger.debug ("Took geocoder work %s from queue.", snapshot_file)
            if not snapshot_file:
                continue
            if snapshot_file == 'done':
                logger.info ("Ending point in poly worker process.")
                break
            columns = None
            with open (snapshot_file, 'rb') as stream:
                line = stream.readline ()
                if line.startswith ("description"):
                    line = stream.readline ()
                columns = line.strip().split ('\t')
                output_file_path = form_output_select_file_path (output_dir, snapshot_file)
                if not os.path.exists (output_file_path):
                    data_importer = DataImporter (query, columns, database, output_dir)
                    data_importer.data_import (snapshot_file, output_file_path, fips_county_map)
        except Queue.Empty:
            pass

def select (query, database, snapshotDB, output_dir, fips_county_map):
    ''' Start workers - one per cpu - to import data and select items.'''
    workers = []
    num_workers = multiprocessing.cpu_count ()
    work_Q = multiprocessing.Queue () 
    logger.info ("Starting %s import/select workers.", num_workers)
    for c in range (num_workers):
        data_import_args = (query, database, output_dir, fips_county_map, work_Q)
        process = multiprocessing.Process (target=select_worker, args = data_import_args)
        workers.append (process)
        
    for process in workers:
        process.start ()

    for root, dirnames, filenames in os.walk (snapshotDB):
        for idx, file_name in enumerate (fnmatch.filter (filenames, 'person.*')):
            snapshot_file = os.path.join (root, file_name)

            base = os.path.basename (snapshot_file)
            last = base.split ('.')[1]
            timeslice = int (last)

            if timeslice > 38:
                logger.debug ("launch-snap[%s] %s", idx, snapshot_file)
                work_Q.put (snapshot_file)
    for worker in workers:
        work_Q.put ('done')
    work_Q.close ()
    work_Q.join_thread ()

    for worker in workers:
        worker.join ()
        logger.info ("Joined process[%s]: %s", worker.pid, worker.name)
        '''
def select_pbs_job (query, database, snapshotDB, output_dir, fips_county_map, snapshots):
    workers = []
    num_workers = multiprocessing.cpu_count ()
    work_Q = multiprocessing.Queue () 
    logger.info ("Starting %s import/select workers.", num_workers)
    for c in range (num_workers):
        data_import_args = (query, database, output_dir, fips_county_map, work_Q)
        process = multiprocessing.Process (target=select_worker, args = data_import_args)
        workers.append (process)
        
    for process in workers:
        process.start ()

    for snapshot_file in snapshots:
        base = os.path.basename (snapshot_file)
        last = base.split ('.')[1]
        timeslice = int (last)
        if timeslice > 38:
            logger.debug ("launch-snap[%s] %s", idx, snapshot_file)
            work_Q.put (snapshot_file)

    for worker in workers:
        work_Q.put ('done')
    work_Q.close ()
    work_Q.join_thread ()

    for worker in workers:
        worker.join ()
        logger.info ("Joined process[%s]: %s", worker.pid, worker.name)
        '''
def calculate_geo_intersection (arguments, snapshot_file):
    logger.info ("getting county information...")
    geocoder = Geocoder ()
    fips_county_map = geocoder.export_polygons (arguments.shapefile, arguments.output)
    with open (arguments.input, 'rb') as stream:
        line = stream.readline ()
        if line.startswith ("description"):
            line = stream.readline ()
        columns = line.strip().split ('\t')
        output_file_path = form_output_select_file_path (arguments.output, arguments.input)
        if not os.path.exists (output_file_path):
            data_importer = DataImporter (arguments.query, columns, arguments.database, arguments.output)
            data_importer.data_import (arguments.input, output_file_path, fips_county_map)

def get_template (file_name):
    template = None
    with open (file_name, 'r') as template:
        template = Template (template.read ())
    return template

def select_pbs (arguments):
    ''' Start workers - one per cpu - to import data and select items.'''

    job_list = []

    script = get_template ("job-template.txt")

    run_dir = "run"
    log_dir = os.path.join (run_dir, "log")
    job_dir = os.path.join (run_dir, "job")
    if not os.path.exists (job_dir):
        os.makedirs (job_dir)
    if not os.path.exists (log_dir):
        os.makedirs (log_dir)
    job_id = 0
    processors = 1
    snapshot_files = []
    for root, dirnames, filenames in os.walk (arguments.snapshotDB):
        for idx, file_name in enumerate (fnmatch.filter (filenames, 'person.*')):
            snapshot_file = os.path.join (root, file_name)
            base = os.path.basename (snapshot_file)
            last = base.split ('.')[1]
            timeslice = int (last)
            if timeslice > 38:
                logger.debug ("launch-snap[%s] %s", idx, snapshot_file)
                snapshot_files.append (snapshot_file)                
                if len (snapshot_files) == 30:
                    write_job (job_dir, job_id, arguments, snapshot_files, script, log_dir, processors, job_list)
                    job_id += 1

    if len (snapshot_files) > 0:
        write_job (job_dir, job_id, arguments, snapshot_files, script, log_dir, processors, job_list)

    job_launcher = os.path.join (job_dir, "launch.sh")
    launcher_template = get_template ('launcher-template.txt')
    with open (job_launcher, 'w') as launcher:
        launcher.write (launcher_template.substitute ({ 'job_dir' : job_dir } ))
    status = os.stat (job_launcher)
    os.chmod (job_launcher, status.st_mode | stat.S_IEXEC)
    subprocess.call ( [ job_launcher ] )

def write_job (job_dir, job_id, arguments, snapshot_files, script, log_dir, processors, job_list):
    logger.debug ( "writing script" )
    text = script.substitute ({
            'job_id'      : job_id,
            'snapshot_db' : arguments.snapshotDB,
            'output_dir'  : arguments.output,
            'shapefile'   : arguments.shapefile,
            'inputs'      : " \n".join (snapshot_files),
            'log'         : log_dir,
            'processors'  : processors
            })
    job_name = os.path.join (job_dir, "job-{0:04d}.sh".format (job_id))
    with open (job_name, 'w') as job:
        job.write (text)
        status = os.stat (job_name)
        os.chmod (job_name, status.st_mode | stat.S_IEXEC)
        snapshot_files [:] = []
    job_list.append (job_name)

def crunch (output_dir, polygon_count):
    logger.debug ("counting %s", output_dir)
    slice_pattern = os.path.join (output_dir, "*.txt")
    slices = glob.glob (slice_pattern)
    counters = {}
    for slice in slices:
        logger.debug ("analyzing slice: %s", slice)
        with open (slice, 'rb') as stream:
            for line in stream:
                metric, scenario, timeslice, county_code, polygon_id, count = line.split ()
                scenario = scenario.split ('.')[1]
                counter = get_counter (counters, metric, scenario, polygon_count)
                count = int(count)
                if count > 0:
                    polygon_id = int(polygon_id)-1
                    timeslice = int(timeslice)
                    logger.debug ("metric: %s scenario: %s timeslice: %s county: %s polygon: %s count: %s",
                                  metric, scenario, timeslice, county_code, polygon_id, count)
                    counter.increment (int(polygon_id)-1, int(timeslice), int(count))

    for i, metric in enumerate (counters):
        for j, scenario in enumerate (counters [metric]):
            counter = counters[metric][scenario]
            #print "---------> metric %s scenario %s repr %s" % (metric, scenario, repr(counter))
            object_name = "%s-%s-occurrences" % (metric, scenario)
            obj = { "counts" : counter.timeline }
            file_name = "%s.json" % os.path.join (output_dir, object_name)
            write_json_object (file_name, obj)

def write_json_object (filename, obj):
    with open (filename, 'w') as stream:
        stream.write (json.dumps (obj, indent=1, sort_keys=True))

def get_counter (counters, metric, scenario, polygon_count):
    '''  Get an existing counter object or construct, store and return a new one if
    no matching counter exists yet.'''
    metric_map = {}
    
    if metric in counters:
        metric_map = counters [metric]
    else:
        counters [metric] = metric_map
        
    scenario_counter = None
    if scenario in metric_map:
        scenario_counter = metric_map [scenario]
    else:
        scenario_counter = Counter (polygon_count)
        metric_map [scenario] = scenario_counter            
        
    return scenario_counter
    

class Counter (object):

    ''' Abstraction of a matrix of counts by polygon over time. '''
    def __init__(self, polygon_count, timeslices = 100):
        self.timeline = [
            [ 0 for i in range (timeslices + 1) ] for j in range (polygon_count + 1)
            ]
        self.increment_count = 0

    def increment (self, polygon_id, timeslice, value=1):
        ''' Count a hit at the polygon and timeslice '''
        self.increment_count += 1
        try:
            if self.increment_count % 1000 == 0:
                logger.debug ("Counter.increment (increment count: %s) (polygon_id=>%s, timeslice=>%s)",
                              self.increment_count,
                              polygon_id,
                              timeslice)
            self.timeline [polygon_id][timeslice] += value
        except IndexError:
            logger.error ("Index Error: Counter.increment (polygon_id=>%s, timeslice=>%s)", polygon_id, timeslice)

    def get (self, polygon_id, timeslice):
        val = None
        try:
            val = self.timeline [polygon_id][timeslice]
        except IndexError:
            pass
        return val

    def __str__ (self):
        return str (self.timeline)
    '''
    def __repr__ (self):
        return str (self.timeline)
        '''


class Geocoder (object):
    ''' Geocode point data to a set of polygons. '''
    def export_polygons (self, shapefile, output_dir):
        ''' Write the list of polygons as json. '''
        fips_map = []
        polygon_count = -1
        from fiona import collection
        with fiona.open (shapefile, "r") as stream:
            for feature in stream:
                #print feature['properties']
                county_name = feature['properties']['NAME10']
                fips_county_code = feature['properties']['COUNTYFP10']
                fips_map.append (fips_county_code)
                #print "%s : %s" % (county_name, fips_county_code)

                polygon_count = int (feature ['id'])
                geometry = feature ['geometry']
                obj = {
                    'count' : polygon_count,
                    'points' : geometry ['coordinates'][0]
                    }
        return fips_map

def form_output_select_file_path (output_dir, file_name):
    path = file_name.split (os.path.sep)
    path = "_".join (path [-2:])
    output_file_path = os.path.join (output_dir, path)
    if not output_file_path.endswith (".txt"):
        output_file_path = "%s.txt" % output_file_path
    return output_file_path

def archive (archive_name = "out.tar.gz"):
    ''' Archive and remove the output files. '''
    logger.info ("Creating output archive: %s", archive_name)
    with tarfile.open (archive_name, "w:gz") as archive:
        files = glob.glob ("*.json")
        for output in files:
            logger.debug ("   archive + %s", output)
            archive.add (output)
        for output in files:
            os.remove (output)

def signal_handler (signum, frame):
    logger.info ('Signal handler called with signal: %s', signum)
    sys.exit (0)

class GeocodeArguments (object):

    def __init__ (self):
        self.root = os.path.join ( os.path.sep, "projects", "systemsscience" )
        self.shapefile = self.form_data_path ([ "var", "census2010", "tl_2010_37_county10.shp" ])
        self.snapshotDB = self.form_data_path ("out")
        self.input = None
        self.output = "output"
        self.count = False
        self.select = False
        self.database = ":memory:"
        self.loglevel = "error"
        self.archive = False

    def form_data_path (self, args):
        tail = os.path.join (*args) if isinstance (args, list) else args
        return os.path.join (self.root, tail)

def process_pipeline (arguments = GeocodeArguments (), callback = None):

    ''' Configure logging. '''
    numeric_level = getattr (logging, arguments.loglevel.upper (), None)
    assert isinstance (numeric_level, int), "Undefined log level: %s" % arguments.loglevel
    logging.basicConfig (level=numeric_level, format='%(asctime)-15s %(message)s')

    logger.info (" shapefile: %s", arguments.shapefile)
    logger.info ("snapshotDB: %s", arguments.snapshotDB)
    if arguments.input:
        logger.info ("    input: %s", arguments.input)
    logger.info ("    output: %s", arguments.output)
    logger.info ("  loglevel: %s", arguments.loglevel)

    logger.info ("Geocoding...")
    geocoder = Geocoder ()
    fips_county_map = geocoder.export_polygons (arguments.shapefile, arguments.output)

    logger.info ("Select...")
    
    select (arguments.query,
            arguments.database,
            arguments.snapshotDB,
            arguments.output,
            fips_county_map)

    exported_polygons = os.path.join (arguments.output, "polygon*json")

    if arguments.archive:
        archive ()
        if callback:
            callback ('out.tar.gz')
        
def process_pipeline_pbs (arguments = GeocodeArguments ()):

    ''' Configure logging. '''
    numeric_level = getattr (logging, arguments.loglevel.upper (), None)
    assert isinstance (numeric_level, int), "Undefined log level: %s" % arguments.loglevel
    logging.basicConfig (level=numeric_level, format='%(asctime)-15s %(message)s')

    logger.info (" shapefile: %s", arguments.shapefile)
    logger.info ("snapshotDB: %s", arguments.snapshotDB)
    if arguments.input:
        logger.info ("    input: %s", arguments.input)
    logger.info ("    output: %s", arguments.output)
    logger.info ("  loglevel: %s", arguments.loglevel)

    if arguments.input:
        logger.info ("    input: %s", arguments.input)
        geocoder = Geocoder ()
        fips_county_map = geocoder.export_polygons (arguments.shapefile, arguments.output)
        calculate_geo_intersection (arguments, fips_county_map)
    elif not arguments.count:
        select_pbs (arguments)
        exported_polygons = os.path.join (arguments.output, "polygon*json")        
    else:
        geocoder = Geocoder ()
        fips_county_map = geocoder.export_polygons (arguments.shapefile, arguments.output)
        crunch (arguments.output, len (fips_county_map))
    
    if arguments.archive:
        archive ()

def main ():

    ''' Register signal handler. '''
    signal.signal (signal.SIGINT, signal_handler)

    parameters = GeocodeArguments ()

    ''' Parse arguments. '''
    parser = argparse.ArgumentParser ()
    parser.add_argument ("--shapefile",    help="Path to an ESRI shapefile", default=parameters.shapefile)
    parser.add_argument ("--snapshotDB",   help="Path to a directory hierarchy containing population snapshot files.", default=parameters.snapshotDB)
    parser.add_argument ("--input",        help="A specific snapshot file to process.", default=parameters.input)
    parser.add_argument ("--output",       help="Output directory. Default is 'output'.", default=parameters.output)
    parser.add_argument ("--select",       help="Select.", action='store_true', default=parameters.select)
    parser.add_argument ("--count",        help="Count.", action='store_true', default=parameters.count)
    parser.add_argument ("--database",     help="Database path. Default is in-memory.", default=parameters.database)
    parser.add_argument ("--loglevel",     help="Log level", default=parameters.loglevel)
    parser.add_argument ("--archive",      help="Archive output files.", dest='archive', action='store_true', default=parameters.archive)
    args = parser.parse_args ()

    parameters.shapefile = args.shapefile
    parameters.snapshotDB = args.snapshotDB
    parameters.input = args.input
    parameters.output = args.output
    parameters.count = args.select
    parameters.count = args.count
    parameters.database = args.database
    parameters.loglevel = args.loglevel
    parameters.archive = args.archive
    parameters.query = {
        "crc"         : "select count(*) from simulation where cancer_free_years > 0.0 and stcotrbg like '37{0}%' ",
        "colonoscopy" : "select count(*) from simulation where num_colonoscopies > 0.0 and stcotrbg like '37{0}%' "
        }

    process_pipeline_pbs (parameters)

    sys.exit (0)

if __name__ == '__main__':
    fs_lock = Lock ()
    main ()


