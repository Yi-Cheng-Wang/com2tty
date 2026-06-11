import logging
import threading
import time
import serial
import serial.rfc2217

class Redirector(object):
    def __init__(self, serial_instance, socket_conn, debug=False):
        self.serial = serial_instance
        self.socket = socket_conn
        self._write_lock = threading.Lock()
        self.rfc2217 = serial.rfc2217.PortManager(
            self.serial,
            self,
            logger=logging.getLogger('rfc2217.server') if debug else None)
        self.log = logging.getLogger('redirector')
        self.alive = False

    def statusline_poller(self):
        self.log.debug('status line poll thread started')
        while self.alive:
            time.sleep(1)
            try:
                self.rfc2217.check_modem_lines()
            except Exception:
                pass
        self.log.debug('status line poll thread terminated')

    def shortcircuit(self):
        """connect the serial port to the TCP port by copying everything"""
        self.alive = True
        self.thread_read = threading.Thread(target=self.reader)
        self.thread_read.daemon = True
        self.thread_read.name = 'serial->socket'
        self.thread_read.start()
        
        self.thread_poll = threading.Thread(target=self.statusline_poller)
        self.thread_poll.daemon = True
        self.thread_poll.name = 'status line poll'
        self.thread_poll.start()
        
        self.writer()

    def reader(self):
        """loop forever and copy serial->socket"""
        self.log.debug('reader thread started')
        while self.alive:
            try:
                # We use in_waiting to read chunks if available
                # Fallback to 1 byte if nothing waiting, relies on ser.timeout
                bytes_to_read = self.serial.in_waiting or 1
                data = self.serial.read(bytes_to_read)
                if data:
                    self.write(b''.join(self.rfc2217.escape(data)))
            except Exception as msg:
                if self.alive:
                    self.log.debug(f'Reader error: {msg}')
                break
        self.alive = False
        self.log.debug('reader thread terminated')

    def write(self, data):
        """thread safe socket write with no data escaping"""
        with self._write_lock:
            try:
                self.socket.sendall(data)
            except Exception:
                pass

    def writer(self):
        """loop forever and copy socket->serial"""
        while self.alive:
            try:
                data = self.socket.recv(1024)
                if not data:
                    break
                self.serial.write(b''.join(self.rfc2217.filter(data)))
            except Exception as msg:
                if self.alive:
                    self.log.debug(f'Writer error: {msg}')
                break
        self.stop()

    def stop(self):
        """Stop copying"""
        self.log.debug('stopping redirector')
        if self.alive:
            self.alive = False
