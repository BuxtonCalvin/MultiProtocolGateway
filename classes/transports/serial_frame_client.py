# Description: scraper transport class for serial communication with SOI/EOI framing, supporting both synchronous and asynchronous modes. Does not implement any protocol-specific logic, just the framing and serial communication. Can...
# File: serial_frame_client.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

# scraper transport class for serial communication with SOI/EOI framing, supporting both synchronous and asynchronous modes.
# Does not implement any protocol-specific logic, just the framing and serial communication. Can be used as a base
# for custom serial protocols that use simple start/end framing.
import threading
import time
from typing import Any, Callable

import serial


class serial_frame_client():
    transport_type = "base class"
    ''' basic serial client implementing an empty SOI/EOI frame'''
    client : serial.Serial
    running : bool = False
    soi : bytes
    '''start of information'''
    eoi : bytes
    '''end of information'''
    pending_frames : list[bytes] = []

    max_frame_size : int = 256

    port : str = "/dev/ttyUSB0"
    baud :  int = 9600

    timeout : float = 5
    ''' timeout in seconds '''

    #region asynchronous
    asynchronous : bool = False
    ''' if set, runs main loop'''

    on_message : Callable[[bytes], None] | None = None
    ''' async mode only'''

    thread : threading.Thread
    ''' main thread for read loop'''

    callback_lock : threading.Lock = threading.Lock()
    '''lock for callback'''
    #endregion asynchronous


    def __init__(self, port : str , baud : int , soi : bytes, eoi : bytes, **kwrgs: Any) -> None:
        self.soi = soi
        self.eoi = eoi
        self.port = port
        self.baud = baud
        self.client = serial.Serial(port, baud, **kwrgs)

    def connect(self) -> bool:
        if self.asynchronous:
            self.running = True
            self.pending_frames = []
            self.thread = threading.Thread(target=self.read_thread, name="Serial_Read", daemon=True)
            self.thread.daemon = True
            self.thread.start()
        return True

    def write(self, data : bytes) -> None:
        ''' write data, excluding SOI and EOI bytes'''
        data = self.soi + data + self.eoi
        self.client.write(data)

    def read(self, reset_buffer: bool = True, frames: int = 1) -> list[bytes] | bytes | None:
        buffer = bytearray()
        self.pending_frames.clear()

        if reset_buffer:
            self.client.reset_input_buffer()

        timedout = time.time() + self.timeout
        self.client.timeout = self.timeout

        while time.time() < timedout:
            data = self.client.read()

            if data:
                buffer += data
                soi_index = buffer.find(self.soi)

                while soi_index != -1:
                    buffer = buffer[soi_index:]
                    eoi_index = buffer.find(self.eoi)

                    if eoi_index != -1:
                        frame = buffer[len(self.soi):eoi_index]

                        if frames == 1:
                            return bytes(frame)

                        # Accumulate frames and return when we have enough
                        self.pending_frames.append(frame)
                        if len(self.pending_frames) == frames:
                            return self.pending_frames

                        buffer = buffer[eoi_index + len(self.eoi):]
                        soi_index = buffer.find(self.soi)

                    else:
                        if len(buffer) > self.max_frame_size:
                            buffer.clear()
                        break

            time.sleep(0.01)

        # Timeout reached — return whatever was collected (could be empty or partial)
        return self.pending_frames if self.pending_frames else None


    def read_thread(self) -> None:
        buffer = bytearray()
        self.running = True
        while self.running:
            # Read data from serial port
            data = self.client.read()

            # Check if data is available
            if data:
                # Append data to buffer
                buffer += data

                # Find SOI index in buffer
                soi_index = buffer.find(self.soi)

                # Process all occurrences of SOI in buffer
                while soi_index != -1:
                    # Remove data before SOI sequence
                    buffer = buffer[soi_index:]

                    # Find EOI index in buffer
                    eoi_index = buffer.find(self.eoi)

                    if eoi_index != -1:
                        # Extract and store the complete frame
                        self.pending_frames.append(buffer[len(self.soi):eoi_index])

                        # Remove the processed data from the buffer
                        buffer = buffer[eoi_index + len(self.eoi) : ]

                        # Find next SOI index in the remaining buffer
                        soi_index = buffer.find(self.soi)
                    else:
                        # If no EOI is found and buffer size exceeds max_frame_size, clear buffer
                        if len(buffer) > self.max_frame_size:
                            buffer.clear()
                        break #no eoi, continue waiting

                #can probably be in the loop, but being cautious
                for frame in self.pending_frames:
                    with self.callback_lock:
                        if self.on_message:
                            self.on_message(frame)

            time.sleep(0.01)


