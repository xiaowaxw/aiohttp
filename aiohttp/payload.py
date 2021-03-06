import io
import json
import mimetypes
import os
import warnings
from abc import ABC, abstractmethod

from multidict import CIMultiDict

from . import hdrs
from .helpers import (PY_36, content_disposition_header, guess_filename,
                      parse_mimetype, sentinel)
from .streams import DEFAULT_LIMIT


__all__ = ('PAYLOAD_REGISTRY', 'get_payload', 'payload_type', 'Payload',
           'BytesPayload', 'StringPayload',
           'IOBasePayload', 'BytesIOPayload', 'BufferedReaderPayload',
           'TextIOPayload', 'StringIOPayload', 'JsonPayload')

TOO_LARGE_BYTES_BODY = 2 ** 20


class LookupError(Exception):
    pass


def get_payload(data, *args, **kwargs):
    return PAYLOAD_REGISTRY.get(data, *args, **kwargs)


def register_payload(factory, type):
    PAYLOAD_REGISTRY.register(factory, type)


class payload_type:

    def __init__(self, type):
        self.type = type

    def __call__(self, factory):
        register_payload(factory, self.type)
        return factory


class PayloadRegistry:
    """Payload registry.

    note: we need zope.interface for more efficient adapter search
    """

    def __init__(self):
        self._registry = []

    def get(self, data, *args, **kwargs):
        if isinstance(data, Payload):
            return data
        for factory, type in self._registry:
            if isinstance(data, type):
                return factory(data, *args, **kwargs)

        raise LookupError()

    def register(self, factory, type):
        self._registry.append((factory, type))


class Payload(ABC):

    _size = None
    _headers = None
    _content_type = 'application/octet-stream'

    def __init__(self, value, *, headers=None, content_type=sentinel,
                 filename=None, encoding=None, **kwargs):
        self._value = value
        self._encoding = encoding
        self._filename = filename
        if headers is not None:
            self._headers = CIMultiDict(headers)
            if content_type is sentinel and hdrs.CONTENT_TYPE in self._headers:
                content_type = self._headers[hdrs.CONTENT_TYPE]

        if content_type is sentinel:
            content_type = None

        self._content_type = content_type

    @property
    def size(self):
        """Size of the payload."""
        return self._size

    @property
    def filename(self):
        """Filename of the payload."""
        return self._filename

    @property
    def headers(self):
        """Custom item headers"""
        return self._headers

    @property
    def encoding(self):
        """Payload encoding"""
        return self._encoding

    @property
    def content_type(self):
        """Content type"""
        if self._content_type is not None:
            return self._content_type
        elif self._filename is not None:
            mime = mimetypes.guess_type(self._filename)[0]
            return 'application/octet-stream' if mime is None else mime
        else:
            return Payload._content_type

    def set_content_disposition(self, disptype, quote_fields=True, **params):
        """Sets ``Content-Disposition`` header."""
        if self._headers is None:
            self._headers = CIMultiDict()

        self._headers[hdrs.CONTENT_DISPOSITION] = content_disposition_header(
            disptype, quote_fields=quote_fields, **params)

    @abstractmethod
    async def write(self, writer):
        """Write payload.

        writer is an AbstractPayloadWriter instance:
        """


class BytesPayload(Payload):

    def __init__(self, value, *args, **kwargs):
        assert isinstance(value, (bytes, bytearray, memoryview)), \
            "value argument must be byte-ish (%r)" % type(value)

        if 'content_type' not in kwargs:
            kwargs['content_type'] = 'application/octet-stream'

        super().__init__(value, *args, **kwargs)

        self._size = len(value)

        if self._size > TOO_LARGE_BYTES_BODY:
            if PY_36:
                kwargs = {'source': self}
            else:
                kwargs = {}
            warnings.warn("Sending a large body directly with raw bytes might"
                          " lock the event loop. You should probably pass an "
                          "io.BytesIO object instead", ResourceWarning,
                          **kwargs)

    async def write(self, writer):
        await writer.write(self._value)


class StringPayload(BytesPayload):

    def __init__(self, value, *args,
                 encoding=None, content_type=None, **kwargs):

        if encoding is None:
            if content_type is None:
                encoding = 'utf-8'
                content_type = 'text/plain; charset=utf-8'
            else:
                mimetype = parse_mimetype(content_type)
                encoding = mimetype.parameters.get('charset', 'utf-8')
        else:
            if content_type is None:
                content_type = 'text/plain; charset=%s' % encoding

        super().__init__(
            value.encode(encoding),
            encoding=encoding, content_type=content_type, *args, **kwargs)


class StringIOPayload(StringPayload):

    def __init__(self, value, *args, **kwargs):
        super().__init__(value.read(), *args, **kwargs)


class IOBasePayload(Payload):

    def __init__(self, value, disposition='attachment', *args, **kwargs):
        if 'filename' not in kwargs:
            kwargs['filename'] = guess_filename(value)

        super().__init__(value, *args, **kwargs)

        if self._filename is not None and disposition is not None:
            self.set_content_disposition(disposition, filename=self._filename)

    async def write(self, writer):
        try:
            chunk = self._value.read(DEFAULT_LIMIT)
            while chunk:
                await writer.write(chunk)
                chunk = self._value.read(DEFAULT_LIMIT)
        finally:
            self._value.close()


class TextIOPayload(IOBasePayload):

    def __init__(self, value, *args,
                 encoding=None, content_type=None, **kwargs):

        if encoding is None:
            if content_type is None:
                encoding = 'utf-8'
                content_type = 'text/plain; charset=utf-8'
            else:
                mimetype = parse_mimetype(content_type)
                encoding = mimetype.parameters.get('charset', 'utf-8')
        else:
            if content_type is None:
                content_type = 'text/plain; charset=%s' % encoding

        super().__init__(
            value,
            content_type=content_type, encoding=encoding, *args, **kwargs)

    @property
    def size(self):
        try:
            return os.fstat(self._value.fileno()).st_size - self._value.tell()
        except OSError:
            return None

    async def write(self, writer):
        try:
            chunk = self._value.read(DEFAULT_LIMIT)
            while chunk:
                await writer.write(chunk.encode(self._encoding))
                chunk = self._value.read(DEFAULT_LIMIT)
        finally:
            self._value.close()


class BytesIOPayload(IOBasePayload):

    @property
    def size(self):
        position = self._value.tell()
        end = self._value.seek(0, os.SEEK_END)
        self._value.seek(position)
        return end - position


class BufferedReaderPayload(IOBasePayload):

    @property
    def size(self):
        try:
            return os.fstat(self._value.fileno()).st_size - self._value.tell()
        except OSError:
            # data.fileno() is not supported, e.g.
            # io.BufferedReader(io.BytesIO(b'data'))
            return None


class JsonPayload(BytesPayload):

    def __init__(self, value,
                 encoding='utf-8', content_type='application/json',
                 dumps=json.dumps, *args, **kwargs):

        super().__init__(
            dumps(value).encode(encoding),
            content_type=content_type, encoding=encoding, *args, **kwargs)


PAYLOAD_REGISTRY = PayloadRegistry()
PAYLOAD_REGISTRY.register(BytesPayload, (bytes, bytearray, memoryview))
PAYLOAD_REGISTRY.register(StringPayload, str)
PAYLOAD_REGISTRY.register(StringIOPayload, io.StringIO)
PAYLOAD_REGISTRY.register(TextIOPayload, io.TextIOBase)
PAYLOAD_REGISTRY.register(BytesIOPayload, io.BytesIO)
PAYLOAD_REGISTRY.register(
    BufferedReaderPayload, (io.BufferedReader, io.BufferedRandom))
PAYLOAD_REGISTRY.register(IOBasePayload, io.IOBase)
