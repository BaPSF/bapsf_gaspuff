# bapsf_gaspuff

```mermaid
classDiagram
    Device -- PiezoValve
    Device -- Flowmeter
    Device -- PressureTransducer
```

```mermaid
flowchart LR
    subgraph gas[gas puff]
    direction LR
    ADC[AD/DA board] -->|write| amp[Amplifier]
    amp --> piezo[Piezo valve]
    amp --> |X10 probe|scope
    psi[Pressure transducer] -->|read| ADC
    end

    subgraph central[" "]
    in["input.txt"]-->Pi[Raspberry Pi]
    Pi-->out["output.txt"]
    Pi ---|serial| flow[Flowmeter]
    Pi ---|GPIO pin| Trigger
    flow .-> out
    end

    in .-> ADC
    ADC .-> out
    Pi --- ADC
```
