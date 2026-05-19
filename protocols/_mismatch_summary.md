# Register / codes mismatch summary

- **Matched:** 179
- **JSON `_codes` without CSV register:** 21
- **CSV fault/alarm rows without JSON `_codes`:** 163

## JSON codes without matching CSV register

| Protocol | JSON key | Similar CSV (reg:name) |
| --- | --- | --- |
| growatt/growatt_2020_v1.24 | `bafdistatus_codes` | — |
| growatt/growatt_bms_rs485_1xsxxp_ess_v2.01 | `status_bits_codes` | — |
| growatt/growatt_bms_rs485_1xsxxp_ess_v2.01 | `error_codes` | — |
| pylon/pylon_rs485_v3.3 | `protection_codes` | — |
| pylon/pylon_rs485_v3.3 | `alarm_codes` | x4644:battery_1_alarm |
| sigineer/sigineer_v0.11 | `Solar_BatVoltConsistFlag_codes` | 221:solar_batvoltconsistfl_ag |
| sma/sma_sunny_island_v1 | `status_condition_codes` | — |
| sma/sma_sunny_island_v1 | `grid_relay_status_codes` | — |
| sma/sma_sunny_island_v1 | `backup_mode_active_codes` | — |
| sma/sma_sunny_island_v1 | `generator_request_codes` | — |
| sma/sma_sunny_island_v1 | `operating_mode_codes` | — |
| sok/sok_sk48v100_pace_bms | `warning_flag_codes` | — |
| sok/sok_sk48v100_pace_bms | `fault_flag_codes` | — |
| sok/sok_sk48v100_pace_bms | `status_flag_codes` | — |
| victron/victron_multiplus_quattro | `switch_mode_codes` | — |
| victron/victron_venus_gx_system | `system/relay/0/state_codes` | — |
| victron/victron_venus_gx_system | `system/dc/battery/state_codes` | — |
| voltronic/voltronic_bms_2020_03_25 | `cell_voltage_state_codes` | — |
| voltronic/voltronic_bms_2020_03_25 | `bms_temperature_state_codes` | — |
| voltronic/voltronic_bms_v1.1 | `cell_voltage_state_codes` | — |
| voltronic/voltronic_bms_v1.1 | `bms_temperature_state_codes` | — |

## CSV fault/alarm registers without JSON codes

### eg4/eg4_18kpv (22)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 11.b1 | `resetsetting_alltodefault` | `resetsetting_alltodefault_codes` |
| 11.b3 | `resetsetting_faultrecordclr` | `resetsetting_faultrecordclr_codes` |
| 179.b2 | `ufunctionen2_afcialarmclr` | `ufunctionen2_afcialarmclr_codes` |
| 6 | `internalfault` | `internalfault_codes` |
| 73 | `uwautotestdefault_time` | `uwautotestdefault_time_codes` |
| 144.b0 | `afciflag_arcalarmch1` | `afciflag_arcalarmch1_codes` |
| 144.b1 | `afciflag_arcalarmch2` | `afciflag_arcalarmch2_codes` |
| 144.b2 | `afciflag_arcalarmch3` | `afciflag_arcalarmch3_codes` |
| 144.b3 | `afciflag_arcalarmch4` | `afciflag_arcalarmch4_codes` |
| 144.b8 | `afci_arcalarm_rsvd` | `afci_arcalarm_rsvd_codes` |
| 400 | `faultrecord1_yandm` | `faultrecord1_yandm_codes` |
| 401 | `faultrecord1_dandh` | `faultrecord1_dandh_codes` |
| 402 | `faultrecord1_mands` | `faultrecord1_mands_codes` |
| 403 | `faultrecord1_code` | `faultrecord1_code_codes` |
| 404 | `faultrecord1_value` | `faultrecord1_value_codes` |
| 405 | `faultrecord1_setorclr` | `faultrecord1_setorclr_codes` |
| 406 | `faultrecord2_yandm` | `faultrecord2_yandm_codes` |
| 407 | `faultrecord2_dandh` | `faultrecord2_dandh_codes` |
| 408 | `faultrecord2_mands` | `faultrecord2_mands_codes` |
| 409 | `faultrecord2_code` | `faultrecord2_code_codes` |
| 410 | `faultrecord2_value` | `faultrecord2_value_codes` |
| 411 | `faultrecord2_setorclr` | `faultrecord2_setorclr_codes` |

### eg4/eg4_3000ehv_v1 (1)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 426 | `exit_the_fault_mode` | `exit_the_fault_mode_codes` |

### eg4/eg4_v58 (23)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 11.b1 | `resetsetting_alltodefault` | `resetsetting_alltodefault_codes` |
| 11.b3 | `resetsetting_faultrecordclr` | `resetsetting_faultrecordclr_codes` |
| 179.b2 | `ufunctionen2_afcialarmclr` | `ufunctionen2_afcialarmclr_codes` |
| 6 | `internalfault` | `internalfault_codes` |
| 73 | `uwautotestdefault_time` | `uwautotestdefault_time_codes` |
| 144.b0 | `afciflag_arcalarmch1` | `afciflag_arcalarmch1_codes` |
| 144.b1 | `afciflag_arcalarmch2` | `afciflag_arcalarmch2_codes` |
| 144.b2 | `afciflag_arcalarmch3` | `afciflag_arcalarmch3_codes` |
| 144.b3 | `afciflag_arcalarmch4` | `afciflag_arcalarmch4_codes` |
| 144.b8 | `afci_arcalarm_rsvd` | `afci_arcalarm_rsvd_codes` |
| 400 | `faultrecord1_yandm` | `faultrecord1_yandm_codes` |
| 401 | `faultrecord1_dandh` | `faultrecord1_dandh_codes` |
| 402 | `faultrecord1_mands` | `faultrecord1_mands_codes` |
| 403 | `faultrecord1_code` | `faultrecord1_code_codes` |
| 404 | `faultrecord1_value` | `faultrecord1_value_codes` |
| 405 | `faultrecord1_setorclr` | `faultrecord1_setorclr_codes` |
| 406 | `faultrecord2_yandm` | `faultrecord2_yandm_codes` |
| 407 | `faultrecord2_dandh` | `faultrecord2_dandh_codes` |
| 408 | `faultrecord2_mands` | `faultrecord2_mands_codes` |
| 409 | `faultrecord2_code` | `faultrecord2_code_codes` |
| 410 | `faultrecord2_value` | `faultrecord2_value_codes` |
| 411 | `faultrecord2_setorclr` | `faultrecord2_setorclr_codes` |
| 997 | `faultrecord100_code` | `faultrecord100_code_codes` |

### enphase/enphase_iq_gateway_sunspec (2)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 40108 | `event_flags_1` | `event_flags_1_codes` |
| 40110 | `event_flags_2` | `event_flags_2_codes` |

### fronius/fronius_sunspec (1)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 215 | `reset_event_flags` | `reset_event_flags_codes` |

### growatt/growatt_2020_v1.24 (32)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 81 | `pv_voltage_high_fault` | `pv_voltage_high_fault_codes` |
| 3025 | `vbatwarning` | `vbatwarning_codes` |
| 3105 | `fault_maincode` | `fault_maincode_codes` |
| 115 | `binvallfaultcode` | `binvallfaultcode_codes` |
| 990 | `pv_warning_value` | `pv_warning_value_codes` |
| 180 | `dsp075_warning_value` | `dsp075_warning_value_codes` |
| 181 | `dsp075_fault_value` | `dsp075_fault_value_codes` |
| 229 | `bfanfaultbit` | `bfanfaultbit_codes` |
| 1001 | `systemfault_word0` | `systemfault_word0_codes` |
| 1002 | `systemfault_word1` | `systemfault_word1_codes` |
| 1003 | `systemfault_word2` | `systemfault_word2_codes` |
| 1004 | `systemfault_word3` | `systemfault_word3_codes` |
| 1005 | `systemfault_word4` | `systemfault_word4_codes` |
| 1006 | `systemfault_word5` | `systemfault_word5_codes` |
| 1007 | `systemfault_word6` | `systemfault_word6_codes` |
| 1008 | `systemfault_word7` | `systemfault_word7_codes` |
| 1157 | `firstbattfaultsn` | `firstbattfaultsn_codes` |
| 1158 | `second_battfaultsn` | `second_battfaultsn_codes` |
| 1159 | `third_battfaultsn` | `third_battfaultsn_codes` |
| 1160 | `fourth_battfaultsn` | `fourth_battfaultsn_codes` |
| 1161 | `battery_history_fault_code_1` | `battery_history_fault_code_1_codes` |
| 1162 | `battery_history_fault_code_2` | `battery_history_fault_code_2_codes` |
| 1163 | `battery_history_fault_code_3` | `battery_history_fault_code_3_codes` |
| 1164 | `battery_history_fault_code_4` | `battery_history_fault_code_4_codes` |
| 1165 | `battery_history_fault_code_5` | `battery_history_fault_code_5_codes` |
| 1166 | `battery_history_fault_code_6` | `battery_history_fault_code_6_codes` |
| 1167 | `battery_history_fault_code_7` | `battery_history_fault_code_7_codes` |
| 1168 | `battery_history_fault_code_8` | `battery_history_fault_code_8_codes` |
| 3107 | `fault_subcode` | `fault_subcode_codes` |
| 3167 | `faultcode` | `faultcode_codes` |
| 3204 | `bmsfault` | `bmsfault_codes` |
| 3205 | `bmsfault2` | `bmsfault2_codes` |

### growatt/growatt_v0.14 (2)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 107 | `audioalarmen` | `audioalarmen_codes` |
| 43 | `warning_value` | `warning_value_codes` |

### pace/pace_bms_v1.3 (14)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 60 | `pack_ov_alarm` | `pack_ov_alarm_codes` |
| 64 | `cell_ov_alarm` | `cell_ov_alarm_codes` |
| 68 | `pack_uv_alarm` | `pack_uv_alarm_codes` |
| 72 | `cell_uv_alarm` | `cell_uv_alarm_codes` |
| 76 | `charging_oc_alarm` | `charging_oc_alarm_codes` |
| 79 | `discharging_oc_alarm` | `discharging_oc_alarm_codes` |
| 84 | `charging_ot_alarm` | `charging_ot_alarm_codes` |
| 87 | `discharging_ot_alarm` | `discharging_ot_alarm_codes` |
| 90 | `charging_ut_alarm` | `charging_ut_alarm_codes` |
| 93 | `discharging_ut_alarm` | `discharging_ut_alarm_codes` |
| 96 | `mosfet_ot_alarm` | `mosfet_ot_alarm_codes` |
| 99 | `environment_ot_alarm` | `environment_ot_alarm_codes` |
| 102 | `environment_ut_alarm` | `environment_ut_alarm_codes` |
| 112 | `soc_alarm_threshold` | `soc_alarm_threshold_codes` |

### pylon/pylon_rs485_v3.3 (1)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| x4644 | `battery_1_alarm` | `battery_1_alarm_codes` |

### sigineer/sigineer_v0.11 (5)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 43 | `warning_value` | `warning_value_codes` |
| 181 | `solar1_faultcode` | `solar1_faultcode_codes` |
| 182 | `solar1_warningcode` | `solar1_warningcode_codes` |
| 201 | `solar2_faultcode` | `solar2_faultcode_codes` |
| 202 | `solar2_warningcode` | `solar2_warningcode_codes` |

### solaredge/solaredge_sunspec (4)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 40109 | `alarm_bitmask_1` | `alarm_bitmask_1_codes` |
| 40111 | `alarm_bitmask_2` | `alarm_bitmask_2_codes` |
| 40113 | `alarm_bitmask_3` | `alarm_bitmask_3_codes` |
| 40115 | `alarm_bitmask_4` | `alarm_bitmask_4_codes` |

### srne/srne_v1.7 (26)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| x200 | `fault_bits_1` | `fault_bits_1_codes` |
| x201 | `fault_bits_2` | `fault_bits_2_codes` |
| x202 | `fault_bits_3` | `fault_bits_3_codes` |
| x203 | `fault_bits_4` | `fault_bits_4_codes` |
| x204 | `fault_code_1` | `fault_code_1_codes` |
| x205 | `fault_code_2` | `fault_code_2_codes` |
| x206 | `fault_code_3` | `fault_code_3_codes` |
| x207 | `fault_code_4` | `fault_code_4_codes` |
| x226 | `inverter_fault_state` | `inverter_fault_state_codes` |
| xE00C | `under_voltage_warning_voltage` | `under_voltage_warning_voltage_codes` |
| xF800~xF80F | `faulthistoryrecord00` | `faulthistoryrecord00_codes` |
| xF810~xF81F | `faulthistoryrecord01` | `faulthistoryrecord01_codes` |
| xF820~xF82F | `faulthistoryrecord02` | `faulthistoryrecord02_codes` |
| xF830~xF83F | `faulthistoryrecord03` | `faulthistoryrecord03_codes` |
| xF840~xF84F | `faulthistoryrecord04` | `faulthistoryrecord04_codes` |
| xF850~xF85F | `faulthistoryrecord05` | `faulthistoryrecord05_codes` |
| xF860~xF86F | `faulthistoryrecord06` | `faulthistoryrecord06_codes` |
| xF870~xF87F | `faulthistoryrecord07` | `faulthistoryrecord07_codes` |
| xF880~xF88F | `faulthistoryrecord08` | `faulthistoryrecord08_codes` |
| xF890~xF89F | `faulthistoryrecord09` | `faulthistoryrecord09_codes` |
| xF8A0~xF8AF | `faulthistoryrecord10` | `faulthistoryrecord10_codes` |
| xF8B0~xF8BF | `faulthistoryrecord11` | `faulthistoryrecord11_codes` |
| xF8C0~xF8CF | `faulthistoryrecord12` | `faulthistoryrecord12_codes` |
| xF8D0~xF8DF | `faulthistoryrecord13` | `faulthistoryrecord13_codes` |
| xF8E0~xF8EF | `faulthistoryrecord14` | `faulthistoryrecord14_codes` |
| xF8F0~xF8FF | `faulthistoryrecord15` | `faulthistoryrecord15_codes` |

### srne/srne_v3.9 (1)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 0xE00C | `under_voltage_warning_level` | `under_voltage_warning_level_codes` |

### victron/victron_gx_generic_canbus (7)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| x35A.0 | `external_alarm_1` | `external_alarm_1_codes` |
| X35A.1 | `external_alarm_2` | `external_alarm_2_codes` |
| X35A.2 | `external_alarm_3` | `external_alarm_3_codes` |
| X35A.3 | `external_warning_1` | `external_warning_1_codes` |
| X35A.4 | `external_warning_2` | `external_warning_2_codes` |
| X35A.5 | `external_warning_3` | `external_warning_3_codes` |
| X35A.6 | `external_warning_4` | `external_warning_4_codes` |

### victron/victron_mk3usb_vebus (1)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| F_WARN | `warning_flags` | `warning_flags_codes` |

### victron/victron_vedirect_serial (5)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| AR | `alarm_reason` | `alarm_reason_codes` |
| H11 | `low_voltage_alarms` | `low_voltage_alarms_codes` |
| H12 | `high_voltage_alarms` | `high_voltage_alarms_codes` |
| H13 | `low_aux_voltage_alarms` | `low_aux_voltage_alarms_codes` |
| H14 | `high_aux_voltage_alarms` | `high_aux_voltage_alarms_codes` |

### voltronic/voltronic_bms_v1.1 (16)

| Register | Normalized name | Suggested JSON key |
| --- | --- | --- |
| 231 | `warning_flag` | `warning_flag_codes` |
| 233 | `status_fault_flag` | `status_fault_flag_codes` |
| 60 | `pack_ov_alarm` | `pack_ov_alarm_codes` |
| 64 | `cell_ov_alarm` | `cell_ov_alarm_codes` |
| 68 | `pack_uv_alarm` | `pack_uv_alarm_codes` |
| 72 | `cell_uv_alarm` | `cell_uv_alarm_codes` |
| 76 | `charging_oc_alarm` | `charging_oc_alarm_codes` |
| 79 | `discharging_oc_alarm` | `discharging_oc_alarm_codes` |
| 84 | `charging_ot_alarm` | `charging_ot_alarm_codes` |
| 87 | `discharging_ot_alarm` | `discharging_ot_alarm_codes` |
| 90 | `charging_ut_alarm` | `charging_ut_alarm_codes` |
| 93 | `discharging_ut_alarm` | `discharging_ut_alarm_codes` |
| 96 | `mosfet_ot_alarm` | `mosfet_ot_alarm_codes` |
| 99 | `environment_ot_alarm` | `environment_ot_alarm_codes` |
| 102 | `environment_ut_alarm` | `environment_ut_alarm_codes` |
| 112 | `soc_alarm_threshold` | `soc_alarm_threshold_codes` |
