# Description: Provides a developer utility for working with MultiProtocolGateway data or simulations.
# File: list_to_json.py
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

import json
import re

# Given string
input_string = "1=Charger Only;2=Inverter Only;3=On;4=Off"
while True:
    user_input = input("Enter Data: ")
    user_input  = re.sub(r"\s+", " ", user_input)
    user_input  = re.sub(r"\:\s+", ":", user_input)
    user_input  = re.sub(r"\s+\:", ":", user_input)

    # Split the string into key-value pairs

    if user_input.find(";") != -1:
        pairs = user_input.split(";")
    elif user_input.find("；") != -1:
        pairs = user_input.split("；")
    elif user_input.find("=") != -1:
        pairs = user_input.split("=")
    else:
        pairs = user_input.split()


    # Create a dictionary from the key-value pairs
    result = {}
    for pair in pairs:
        if not pair:
            continue

        if pair.find("：") != -1:
            key, value = pair.split("：")
        elif pair.find("=") != -1:
            key, value = pair.split("=")
        else:
            key, value = pair.split(":")


        result[key.strip()] = value.strip()

    # Convert the dictionary to JSON
    json_result = json.dumps(result)

    print(json_result)
