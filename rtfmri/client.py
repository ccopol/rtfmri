"""Contains class to interface with GE Scanner.

This is the lowest layer of the rtfmri machinery.

"""
from __future__ import print_function
import re
import ftplib
import base64
import getpass
import os
import socket
import sys
import traceback

import os.path as op
from cStringIO import StringIO
from datetime import datetime

import paramiko
import dicom

import pdb

from profilehooks import timecall

class ScannerClient(object):
    """Client to interface with GE scanner in real-time."""

    def __init__(self, hostname="cnimr", port=21,
                 username="", password="",
                 base_dir="/export/home1/sdc_image_pool/images",
                 ftp_debug_level=0):
        """Inialize the client and connect to the FTP server.

        """
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.base_dir = base_dir

        # Try to connect to the server, but catch errors
        # so that we can test aspects of this class without
        # an active FTP server
        try:
            self.connect()

            #check avoid problems in subclass; should be a better way...
            if ftp_debug_level > 0:
                self.ftp.set_debuglevel(ftp_debug_level)
        except socket.error:
            # Connection refused
            self.ftp = None
            print("Could not connect to FTP server.""")

    def connect(self):
        """Connect to the FTP server."""
        self.ftp = ftplib.FTP()
        self.ftp.connect(host=self.hostname, port=self.port)
        self.ftp.login(user=self.username, passwd=self.password)

    def reconnect(self):
        """Reinitialize FTP connection if it was dropped."""
        try:
            self.ftp.voidcmd("NOOP")
        except ftplib.error_temp:
            # Connection has timed out, so reconnect
            self.connect()

    def close(self):
        """Close the connection to the FTP server (if it exists)."""
        if self.ftp is not None:
            self.ftp.close()

    def __del__(self):
        """Close the connection when the object is destroyed."""
        self.close()

    def list_dir(self, dir):
        """Return (timestamp, size, name) for contents in a directory."""
        self.reconnect()

        # Get an ls style list of items in `dir`
        file_list = []
        self.ftp.dir(dir, file_list.append)

        # Parse the output and return
        return self._parse_dir_output(file_list)

    def _alphanumeric_sort(self, file_list):
        """Sort the file list by name respecting numeric order.

        DICOM filenames are numerically sequential, but not zero-padded.
        The FTP server gives us a list of files in "sorted" order, but
        that means the files are not in sequential order. Fix that here.

        """
        def alphanum_key(entry):
            converted_parts = []
            fname = entry.split()[-1]
            parts = re.split("([0-9]+)", fname)
            for part in parts:
                if part.isdigit():
                    converted_parts.append(int(part))
                else:
                    converted_parts.append(part)
            return converted_parts

        file_list.sort(key=alphanum_key)

    def _parse_dir_output(self, file_list):
        """Parse a UNIX-style ls output from the FTP server."""
        # Sort the file list, respecting alphanumeric order
        self._alphanumeric_sort(file_list)
        file_list = [x for x in file_list if not x.startswith('.') and '.DS_Store' not in x]
        # Now go back through each entry and parse out the useful bits
        contents = []
        for i, entry in enumerate(file_list, 1):
            _, _, _, _, size, month, day, time, name = entry.split()
            year = datetime.now().year

            # If the entry is more than a year old, the `time` entry
            # will be a year. But we don't care about those files for
            # real-time usage, so skip them.
            if ":" not in time:
                continue

            # The ls output only gives us timestamps at minute resolution
            # so we are going to use the index in the list to mock a
            # microsecond timestamp. This assumes that we're getting the
            # directory contents in a meaningful sequential order.
            # That should be true because of a) how the scanner names DICOMS
            # and b) the sorting operation we performed above
            time_str = "{} {} {} {}:{:06d}".format(year, month, day, time, i)

            # Get a unique timestamp for this entry
            timestamp = datetime.strptime(time_str, "%Y %b %d %H:%M:%f")

            # Insert a tuple of (timestamp, size, name) for this entry
            contents.append((timestamp, int(size), name))

        # Return this list sorted, which will be in timestamp order.
        # Because of how timestamps work, this is going to be somewhat
        # incorrect. If we created File A on November 16, 2013 and today
        # is November 15, 2014, a file created today is going to look "older"
        # than File A. However I think this won't be a problem in practice,
        # because the scanner doesn't keep files on it for that long.
        # (But maybe it will be an issue right around New Years?)
        contents.sort()
        return contents

    def _latest_entry(self, dir):
        """Return a path to the most recent entry in `dir`."""
        contents = self.list_dir(dir)

        # Contents should be sorted, so we want last entry
        latest_name = contents[-1][-1]

        # Build and return the full path
        path = op.join(dir, latest_name)
        return path

    @property
    def latest_exam(self):
        """Return a path to the most recent exam directory."""
        # The exam directory should always be two layers deep
        latest_patient = self._latest_entry(self.base_dir)
        return self._latest_entry(latest_patient)

    @property
    def latest_series(self):
        """Return a path to the most recent series directory."""
        # The series directory should always be three layers deep
        return self._latest_entry(self.latest_exam)

    def series_dirs(self, exam_dir=None):
        """Return a list of all series dirs for an exam."""
        if exam_dir is None:
            exam_dir = self.latest_exam

        # Get the list of entries in the exam dir
        exam_contents = self.list_dir(exam_dir)
        series_dirs = [op.join(exam_dir, n) for t, s, n in exam_contents]
        return series_dirs

    def series_files(self, series_dir=None):
        """Return a list of all files for a series."""
        if series_dir is None:
            series_dir = self.latest_series

        # Get the list of entries in the exam dir
        series_contents = self.list_dir(series_dir)
        series_files = [op.join(series_dir, n) for t, s, n in series_contents]
        return series_files

    def series_info(self, series_dir=None):
        """Return a dicts with information about a series."""
        if series_dir is None:
            series_dir = self.latest_series

        # Get a list of all the files for this series
        series_files = self.list_dir(series_dir)

        # This directory could be empty
        if not series_files:
            return {}

        # Build the dictionary based off the first DICOM in the list
        _, _, filename = series_files[0]
        dicom_path = op.join(series_dir, filename)
        first_dicom = self.retrieve_dicom(dicom_path)
        dicom_timestamp = first_dicom.StudyDate + first_dicom.StudyTime
        n_timepoints = getattr(first_dicom, "NumberOfTemporalPositions", 1)

        series_info = {
            "Dicomdir": series_dir,
            "DateTime": datetime.strptime(dicom_timestamp, "%Y%m%d%H%M%S"),
            "Series": first_dicom.SeriesNumber,
            "Description": first_dicom.SeriesDescription,
            "NumTimepoints": n_timepoints,
            "NumAcquisitions": len(series_files),
        }

        return series_info

    def retrieve_file(self, filename):
        """Return a file as a string buffer."""
        self.reconnect()
        buf = StringIO()
        self.ftp.retrbinary("RETR {}".format(filename), buf.write)
        buf.seek(0)
        return buf

    def retrieve_dicom(self, filename):
        """Return a file as a dicom object."""
        try:
            return dicom.filereader.read_file(self.retrieve_file(filename))
        except Exception as e:
            print(filename)
            print(type(e).__name__)
            pdb.set_trace()



class ScannerSFTPClient(ScannerClient):
    """The same as scanner client, but with some modifications to use SFTP"""
    def __init__(self, *args, **kwargs):
        super(ScannerSFTPClient, self).__init__(*args, **kwargs)


    def get_key(self):
        # get host key, if we know one - taken from
        # https://raw.githubusercontent.com/paramiko/paramiko/master/demos/demo_sftp.py
        # Paramiko client configuration

        hostkeytype = None
        hostkey = None
        try:
            host_keys = paramiko.util.load_host_keys(os.path.expanduser('~/.ssh/known_hosts'))
        except IOError:
            try:
                # try ~/ssh/ too, because windows can't have a folder named ~/.ssh/
                host_keys = paramiko.util.load_host_keys(os.path.expanduser('~/ssh/known_hosts'))
            except IOError:
                print('*** Unable to open host keys file')
                host_keys = {}

        if self.hostname in host_keys:
            hostkeytype = host_keys[self.hostname].keys()[0]
            hostkey = host_keys[self.hostname][hostkeytype]
            print('Using host key of type %s' % hostkeytype)

        if hostkey == None:
            pass
            #raise  Exception("No hostkey") # TODO change this to better exception type
        return hostkey

    def set_pkey(self, pkey_path):
        """If using a pkey, as is useful for the test server"""
        self.pkey = paramiko.RSAKey.from_private_key_file(pkey_path)

    def connect(self, UseGSSAPI = True, DoGSSAPIKeyExchange = True):
        """Connect to the FTP server."""
        self.hostkey = self.get_key()

        # TODO add a way to set this.
        self.set_pkey("test.key")
        self.transport = paramiko.Transport((self.hostname, self.port))

        if self.pkey:
            self.transport.connect(pkey=self.pkey)
        else:
            self.transport.connect(self.hostkey, self.username, self.password,
                               gss_host=socket.getfqdn(self.hostname),
                               gss_auth=UseGSSAPI,
                               gss_kex=DoGSSAPIKeyExchange,
                               pkey = pkey)

        self.transport.window_size = 2147483647
        self.transport.packetizer.REKEY_BYTES = pow(2, 40)
        self.transport.packetizer.REKEY_PACKETS = pow(2, 40)

        self.sftp = paramiko.SFTPClient.from_transport(self.transport)


    def reconnect(self):
        """Reinitialize FTP connection if it was dropped."""
        # not sure the best way to implement this yet...
        pass

    def close(self):
        """Close the connection to the SFTP server (if it exists)."""
        if self.sftp is not None:
            self.sftp.close()
            self.transport.close()

    def _parse_dir_output(self, file_list):
        """Parse a UNIX-style ls output from the FTP server."""
        # Sort by modification time; this should work on the scanner.
        file_list = [x for x in file_list if not x[2].startswith('.') and '.DS_Store' not in x[2]]

        #file list consists of tuples of (name, mtime, size)
        #now we sort by size
        file_list.sort(key=lambda x: x[0])
        return file_list

    def list_dir(self, path):
        """Return (timestamp, size, name) for contents in a directory."""

        files = self.sftp.listdir(path)
        file_attr = self.sftp.listdir_attr(path)
        file_list = [(y.st_atime, y.st_size, x) for x, y in zip(files, file_attr)]
        # Parse the output and return
        return self._parse_dir_output(file_list)

    #@timecall
    def retrieve_file(self, filename):
        """Return a file as a buffer."""
        buf = self.sftp.file(filename, mode='r', bufsize=0)
        buf.seek(0)
        return buf

    def _latest_entry(self, dir):
        """Return a path to the most recent entry in `dir`."""
        contents = self.list_dir(dir)

        # Contents should be sorted, so we want last entry
        latest_name = contents[-1][2]

        # Build and return the full path
        path = op.join(dir, latest_name)
        return path

