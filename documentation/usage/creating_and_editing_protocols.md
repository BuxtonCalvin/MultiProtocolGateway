# Creating and Editing Protocols ‐ JSON ‐ CSV

"Protocols" are defined through 2 file types.

The first is .json, which contains the default settings and optional lookup codes.

The second are .csv files that hold the registry or address definitions.

There are four types of modbus registery files: Coils, Discrete, Input and Holding.  Modbus is the standard used by most inverters for serial communication.

| Data-Type | Address-Prefix | Access-Type | Size | Description |
| -- | -- | -- | -- | -- |
| Coil | 0xxxx | Read/Write | 1-bit | Digital ON/OFF states used to control outputs (e.g., relays, lights) |
| Discrete | 1xxxx | Read-Only | 1-bit | Digital status from external devices (e.g., limit switches, safety sensors) |
| Input Register | 3xxxx | Read-Only | 16-bit | Analog measurement values (e.g., temperature, pressure readings) |
| Holding Register | 4xxxx | Read/Write | 16-bit | General-purpose data for analog values, configurations, or setpoints. |

## CSV

You can either edit an existing .csv protocol file and save it to a new name (making sure to place it in a given manufacturer's folder) or you can create a protocol file from scratch. To add a new manufacturer, simply create a new folder with the manufacturer name. There is a create protocol widget in the protocol page of the web server app that will walk you through the basics of creating a new protocol file but all you really need to do is create a text file with the proper column headings.

CSV = comma separated values... spreadsheets.
Delimiter for csv can be , or ; ( not both ).

| register | variable_name | documented_name | unit | data_type | values | writable | read_interval | adjustments | note |
| -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| 17 | | Prec | W | | 0-65535 | | R | | AC charging rectification power (For three phase: R phase) |
| 18 | | IinvRMS | 0.01A | | 0-65535 | | R | | Inverter rms current output (For three phase: R phase) |
| 19 | PowerFactor | PF | | | 0-2000 | | R | "{""High_Low"":""x?(0,1000]->x/1000 x?(1000,2000)->(1000-x)/1000""}" | " Power factor for single phase or R phase of three phase" |

### variable name

Provides a user-friendly name to work with, while retaining the original documented name as a lot of variable names are up to interpretation.

### documented name

Original variable name provided from protocol documentation; when variable name is not specified, this name is used.

### data type

Defines the expected data type for the register / map entry.

| Type | Description |
| -- | -- |
| USHORT | A two byte ( 16 bit ) (unsigned) positive number. For protocols that return 2 byte values, this is the default type. |
| SHORT | A two byte ( 16 bit ) (signed) number  (-32,768 to 32,767). |
| UINT | A four byte ( 32 bit ) positive number. |
| INT | A four byte ( 32 bit ) signed number (positive or negative). |
| 16BIT_FLAGS | two bytes split into 16 bits, each bit represents on/off flag which is defined as b#. this will translate into 16x 0/1s if no "codes" are defined. |
| 8BIT_FLAGS | A single byte split into 8 bit flags. see 16BIT_FLAGS |
| 32BIT_FLAGS | four bytes split into 32 bit flags. see 16BIT_FLAGS |
| #bit | A unsigned number comprised of # of bits. for example, 3bit is a 3 bit positive number (0 to 7). |
| ASCII | ascii text representation of data. |
| ASCII.# | for protocols with an undefined "registry" size, the length can be specified. ie: ASCII.7 will return a 7 character long string. |
| HEX | hex text representation of data. |

### register

Register defines the location, or for other protocols, the main command / id.
The registers are defined in decimal form. A prefix of "x" is acceptable for hexadecimal.

For **ASCII** or other MultiRegister Data Types, register ranges can be defined:

```text
7~14
```

A prefix of "r" will specify that the registers be "read" in backwards order.

```text
r7~14
```

#### register bit

Bit offsets are specified with .b#, # being the bit offset.

```text
#.b#
```

#### register byte

Byte offsets are specified with .#, # being the byte offset.

```text
#.#
```

#### writable

Mainly for registers / entries that support writing, such as the holding register for modbus.

```text
R = Read Only
RD = Read Disabled
W = Write
```

#### values / codes

there are two main purposes for this column.

1. Defines possible values / ranges of values for protocol validation / safety,
for example:
```0~100```

2. Defines the values for flag data types, such as 16BIT_FLAGS; any data type can be used.
The format for these flags is json. These flags / codes can also be defined via the .json file by naming them as such:
"{{document_name}}_codes"

### read interval

Provides a per register read interval; the minimum value is the configured transport read interval.

* **x**: for `read_interval` from transport config * #
* **s**: for plain old seconds
* **ms**: for milliseconds

for example:

```text
[transport.modbus]
read_interval = 7
 ```

```7x```
Would set the read interval for that register to 49 seconds.

```7s```
Would set the read interval to 7s.

```1s```
Because the transport read interval is 7 seconds, the read interval would effectively be 7 seconds.

### adjustments

Adjustments allow special processing of the data from a register based on a manufacturer's recommendations.

#### formulas

This formula will return a computed dynamic value based on whether the read value is is between 0-1000, or 1000-2000

{"High_Low":"x?(0,1000]->x/1000 x?(1000,2000)->(1000-x)/1000"}

#### endian order

To ensure the entry is read with a little endian byte order, add to the adjustment field {"Register_Endian":"little"}
Registers default to big endian.

#### Bit Flag Example

```text
{"b0" : "StandBy", "b1" : "On"}
```

##### MultiBit Flag Example

Some protocols utilize combinations of bits as flags.

```text
{"b0" : "StandBy", "b0&b1" : "Operational", "b1" : "Error" }
```

##### uint / bits / other

```text
{"75" : "Error", "50": "Warning"}
````
