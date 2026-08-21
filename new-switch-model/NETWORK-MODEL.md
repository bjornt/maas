This repo is a playground for refactoring the physical hardware model of MAAS.

There should be a clear separatation between the hardware layer
and the configured network devices. Currently the maas_interfaces table
contains a mix of hardware properties and configured properties.

The [model-old.yaml](./model-old.yaml) file contains examples of the old
model, and [model-new.yaml](./model-new.yaml) contains the new model.

The new model should be backwards-compatible, in the sense that it should be
possible to convert the new model into the old. The new model might contain
additional features, so going from old to new is only needed for the initial
migration.

## Old model

The old model is in maas_interface and has a lot of fields. Let's divide
up the files into, hardware, physical link configuration and workload
configuration

### hardware

  * name
  * mac_address
  * tags
  * firmware_version
  * product
  * vendor
  * interface_speed
  * numa_node_id
  * sriov_max_vf
  * node_config_id
  * switch_id


### physical link configuration

  * name
  * type 
  * params
  * tags
  * vlan_id
  * mac_address
  
  
### workload configuration

  * name
  * type 
  * params
  * tags
  * enabled
  * vlan_id
  * mac_address
  
  
### other

  * acquired
  * mdns_discover_state
  * neighbour_discover_state
