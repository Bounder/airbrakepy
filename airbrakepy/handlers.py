import inspect
import logging
import traceback
import multiprocessing
import urllib2
import sys
import os
from xml.etree.ElementTree import Element, tostring, SubElement
from . import __version__ ,__source_url__, __app_name__

_POISON = "xxxxPOISONxxxx"

class AirbrakeSender(multiprocessing.Process):
    def __init__(self, work_queue, timeout_in_ms, service_url):
        multiprocessing.Process.__init__(self, name="AirbrakeSender")
        self.work_queue = work_queue
        self.timeout_in_seconds = timeout_in_ms / 1000.0
        self.service_url = service_url

    def _handle_error(self):
        ei = sys.exc_info()
        try:
            traceback.print_exception(ei[0], ei[1], ei[2],
                                      file=sys.stderr)
        except IOError:
            pass
        finally:
            del ei

    def run(self):
        global _POISON
        while True:
            try:
                message = self.work_queue.get()
                if message == _POISON:
                    break
                self._sendMessage(message)
            except Exception:
                self._handle_error()

    def _sendHttpRequest(self, headers, message):
        request = urllib2.Request(self.service_url, message, headers)
        try:
            response = urllib2.urlopen(request, timeout=self.timeout_in_seconds)
            print response
            status = response.getcode()
        except urllib2.HTTPError as e:
            print 'foo', e, e.message
            status = e.code
        return status

    def _sendMessage(self, message):
        headers = {"Content-Type": "text/xml"}
        status = self._sendHttpRequest(headers, message)
        if status == 200:
            return

        exceptionMessage = "Unexpected status code {0}".format(str(status))

        if status == 403:
            exceptionMessage = "Unable to send using SSL"
        elif status == 422:
            exceptionMessage = "Invalid XML sent: {0}".format(message)
        elif status == 500:
            exceptionMessage = "Destination server is unavailable. Please check the remote server status."
        elif status == 503:
            exceptionMessage = "Service unavailable. You may be over your quota."

        raise StandardError(exceptionMessage)

_DEFAULT_AIRBRAKE_URL = "http://airbrakeapp.com/notifier_api/v2/notices"
_DEFAULT_ENV_VARIABLES = []

class AirbrakeHandler(logging.Handler):
    def __init__(self, api_key, environment=None, level=logging.ERROR, component_name=None, node_name=None,
                 use_ssl=False, timeout_in_ms=30000, env_variables=_DEFAULT_ENV_VARIABLES,
                 airbrake_url=_DEFAULT_AIRBRAKE_URL):
        logging.Handler.__init__(self, level=level)
        self.api_key = api_key
        self.environment = environment
        self.env_variables = env_variables
        self.component_name = component_name
        self.node_name = node_name
        self.work_queue = multiprocessing.Queue()
        self.work_queue.cancel_join_thread()
        self.worker = AirbrakeSender(self.work_queue, timeout_in_ms, self._serviceUrl(airbrake_url, use_ssl))
        self.worker.start()
        self.logger = logging.getLogger(__name__)

    def emit(self, record):
        try:
            message = self._generate_xml(record)
            self.work_queue.put(message)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Airbrake message queued for delivery")
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)


    def close(self):
        if self.work_queue:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("POISONING QUEUE")
            global _POISON
            self.work_queue.put(_POISON, False)
            self.work_queue.close()
            self.work_queue = None

        if self.worker:
            self.logger.info("Waiting for remaining items to be sent to Airbrake.")
            self.worker.join(timeout=5.0)
            if self.worker.is_alive():
                self.logger.info("AirbrakeSender did not exit in an appropriate amount of time...terminating")
                self.worker.terminate()
            self.worker = None

        logging.Handler.close(self)

    def _serviceUrl(self, airbrake_url, use_ssl):
        if use_ssl:
            return airbrake_url.replace('http://', 'https://', 1)
        else:
            return airbrake_url.replace('https://', 'http://', 1)

    def _generate_xml(self, record):
        exn = None
        trace = None
        if record.exc_info:
            _, exn, trace = record.exc_info

        message = record.getMessage()

        if exn:
            message = "{0}: {1}".format(message, str(exn))

        xml = Element('notice', dict(version='2.3'))
        SubElement(xml, 'api-key').text = self.api_key

        notifier = SubElement(xml, 'notifier')
        SubElement(notifier, 'name').text = __app_name__
        SubElement(notifier, 'version').text = __version__
        SubElement(notifier, 'url').text = __source_url__

        server_env = SubElement(xml, 'server-environment')
        SubElement(server_env, 'environment-name').text = self.environment

        request_xml = SubElement(xml, 'request')
        SubElement(request_xml, 'url').text = ""
        SubElement(request_xml, 'component').text = self.component_name
        
        cgi_data = SubElement(request_xml, 'cgi-data')
        for key, value in os.environ.items():
            if key in self.env_variables:
                SubElement(cgi_data, 'var', dict(key=key)).text = str(value)

        error = SubElement(xml, 'error')
        SubElement(error, 'class').text = exn.__class__.__name__ if exn else 'Unknown'
        SubElement(error, 'message').text = message

        backtrace = SubElement(error, 'backtrace')
        if trace is None:
            SubElement(backtrace, 'line', dict(file=record.pathname,
                                               number=str(record.lineno),
                                               method=record.funcName))
        else:
            for pathname, lineno, funcName, text in traceback.extract_tb(trace):
                SubElement(backtrace, 'line', dict(file=pathname, number=str(lineno), method='%s: %s' % (funcName, text)))
        return tostring(xml)

