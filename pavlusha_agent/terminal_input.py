"""POSIX terminal input adapter; policy and key bindings belong to modules."""
import codecs
import copy
import os
import select
import sys
import termios
import threading

from .core import AgentError


class TerminalInput:
    def __init__(self, stream=None, output=None):
        self.stream=stream or sys.stdin
        self.output=output or sys.stderr
        self.fd=None
        self.original=None
        self.running=None
        self.reader=None
        self.stopped=threading.Event()
        self.editing=threading.Event()
        self.read_lock=threading.Lock()

    def start(self, post_key):
        if not self.stream.isatty():
            raise AgentError('interactive mode requires terminal stdin')
        self.fd=self.stream.fileno()
        self.original=termios.tcgetattr(self.fd)
        self.running=copy.deepcopy(self.original)
        self.running[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL | termios.ISIG)
        self.running[6][termios.VMIN]=1
        self.running[6][termios.VTIME]=0
        termios.tcsetattr(self.fd,termios.TCSANOW,self.running)
        try:
            self.reader=threading.Thread(target=self._read_keys,args=(post_key,),daemon=True)
            self.reader.start()
        except BaseException:
            termios.tcsetattr(self.fd,termios.TCSANOW,self.original)
            raise
        self.output.write('Ctrl+Z: pause; Ctrl+\\: urgent pause. Type after PAUSED.\n')
        self.output.flush()

    def _read_keys(self, post_key):
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        while not self.stopped.is_set():
            if self.editing.wait(.03):
                continue
            ready,_,_=select.select([self.fd],[],[],.05)
            if not ready or self.editing.is_set():
                continue
            with self.read_lock:
                if self.editing.is_set() or self.stopped.is_set():
                    continue
                data=os.read(self.fd,1)
            if not data:
                post_key('eof')
                return
            for char in decoder.decode(data):
                post_key({'\x1a':'ctrl_z','\x1c':'ctrl_backslash','\x03':'ctrl_c'}.get(char,char))

    def read_message(self, prompt, *, require_message=False):
        self.editing.set()
        try:
            with self.read_lock:
                termios.tcflush(self.fd,termios.TCIFLUSH)
                editing=copy.deepcopy(self.original)
                # Read paste delimiters and embedded newlines ourselves: canonical
                # readline would submit the first line and silently discard the rest.
                editing[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL | termios.ISIG)
                editing[0] &= ~termios.ICRNL
                editing[6][termios.VMIN]=1
                editing[6][termios.VTIME]=0
                termios.tcsetattr(self.fd,termios.TCSANOW,editing)
                self.output.write('\x1b[?2004hPAUSED — '+(prompt or 'Введите сообщение; пустой ввод продолжит работу.')+
                    '\nВставленный текст сохраняется целиком; Enter отправляет. /multiline — ввод строк, /send — отправка.\n> ')
                self.output.flush()
                decoder=codecs.getincrementaldecoder('utf-8')('replace')
                chars=[]
                sequence=''
                paste=False
                multiline=False
                line_start=0
                start='\x1b[200~'
                end='\x1b[201~'
                while True:
                    raw=os.read(self.fd,1)
                    if not raw:
                        return None
                    for char in decoder.decode(raw):
                        marker=end if paste else start
                        if sequence or char=='\x1b':
                            sequence+=char
                            if sequence==marker:
                                paste=not paste
                                sequence=''
                                continue
                            if marker.startswith(sequence):
                                continue
                            # Do not execute terminal escapes pasted as input.
                            if paste:
                                chars.extend(sequence)
                            sequence=''
                            continue
                        if paste:
                            chars.append(char)
                            if char=='\n': self.output.write('\n')
                            elif char=='\r': pass
                            elif char.isprintable() or char=='\t': self.output.write(char)
                            self.output.flush()
                            continue
                        if char in ('\x04','\x03'):
                            return None
                        if char in ('\x7f','\b'):
                            if len(chars)>line_start and chars[-1]!='\n':
                                chars.pop(); self.output.write('\b \b'); self.output.flush()
                            continue
                        if char not in ('\r','\n'):
                            if char.isprintable() or char=='\t':
                                chars.append(char); self.output.write(char); self.output.flush()
                            continue
                        self.output.write('\n'); self.output.flush()
                        text=''.join(chars).replace('\r\n','\n').replace('\r','\n')
                        current=text[line_start:]
                        if not multiline and text.strip()=='/multiline':
                            multiline=True; chars=[]; line_start=0
                            self.output.write('Многострочный ввод: /send отдельной строкой отправляет, /quit отменяет.\n> '); self.output.flush()
                            continue
                        if multiline and current.strip() not in ('/send','/quit'):
                            chars.append('\n'); line_start=len(chars)
                            self.output.write('> '); self.output.flush()
                            continue
                        if (current.strip() if multiline else text.strip())=='/quit':
                            return None
                        message=text[:line_start].removesuffix('\n') if multiline else text
                        if not require_message or message.strip():
                            return message
                        chars=[]; line_start=0
                        self.output.write('Нужен ответ; /quit завершает сессию.\n> '); self.output.flush()
        finally:
            try:
                self.output.write('\x1b[?2004l'); self.output.flush()
            finally:
                try:
                    if self.running is not None:
                        termios.tcsetattr(self.fd,termios.TCSANOW,self.running)
                finally:
                    self.editing.clear()

    def close(self):
        self.stopped.set()
        try:
            if self.reader is not None:
                self.reader.join(timeout=1)
                if self.reader.is_alive():
                    raise AgentError('terminal keyboard reader did not stop')
                self.reader=None
        finally:
            if self.original is not None:
                termios.tcsetattr(self.fd,termios.TCSANOW,self.original)
                self.original=None
