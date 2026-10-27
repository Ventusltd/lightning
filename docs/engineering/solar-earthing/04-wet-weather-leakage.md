# 04 · Wet weather, array capacitance and leakage current

> **Lightning notice.** The lightning repo shows *observed optical lightning* from EUMETSAT MTG LI L2-LFL. An observed flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress, or a second fault on an IT network. Nothing in this repo attributes a fault to lightning. Post-event inspection is a diligence recommendation, not a diagnosis.

The owner's concern: **protection and monitoring on floating PV DC systems and IT AC collection networks are usually assessed under dry-weather assumptions, and may behave differently when the arrays are wet.** These are concerns and hypotheses with their evidence, not findings about any site.

**Evidence classes** follow the owner's research of 21 October 2026 [1] (*confirmed*, *reasonably inferred*, *disputed*, *unverified*, *unknown*), carried exactly as it assigns them. Points from the owner's notes of 22 October 2026 [2] are marked *(owner's notes)*.

## 1. Two separate wet-weather effects

The owner's notes separate two effects that are easy to confuse [2]:

1. **More capacitance.** Rain or condensation forms a conductive film on the glass, raising the effective capacitance between the cells and earth. Glass and encapsulant remain dielectrics, so this is not an insulation fault.
2. **Less insulation resistance.** Moisture reaching a defective cable, connector or seal lowers the true resistance to earth and can become a genuine conductive fault.

The notes say they "are distinct effects and should be evaluated separately". Most of this document is about why a protection scheme may not tell them apart.

## 2. How much the capacitance changes

### Dry/wet examples (illustrative)

The owner's notes give measured examples, reported in electrical installation guidance [2]:

| PV installation | Dry capacitance | Wet capacitance |
|---|---|---|
| 1 MW ground-mounted | 5 µF | 10 µF |
| 750 kW ground-mounted | 2 µF | 4 µF |
| 100 kW roof-mounted | 2 µF | 4 µF |

These are **illustrative measurements, not a universal multiplier.** They show roughly twice the capacitance when wet, but the value depends on module construction, area, moisture distribution, mounting, nearby structures and cable geometry [2]. The notes credit them to the Schneider Electric *Electrical Installation Guide*; the original table was not traced here, so its provenance is **unverified**.

### Per-kilowatt figures

The research cites SMA: array capacitance to earth is 60 to 110 nF/kW for crystalline modules and 100 to 160 nF/kW for CdTe, dominated by the water film on the glass when wet; an independent 2024 paper gives 50 to 150 nF/kW [1, 3]. The research's account of the leakage mechanism is **reasonably inferred** [1].

Its illustrative fault model applies SMA's range to a 475 kWp array per inverter: **1.2 µF dry, 36.5 µF front glass wet, 72.7 µF both faces wet** [1]. These are model inputs from stated assumptions, not measurements, and the step is far larger than the ×2 above, which is itself a reason not to rely on any single ratio.

**Disputed:** Bender (about 100 µF for roughly 100 kW) and Schneider (a 1 MWp farm with "only a few points over 110 µF") differ by an order of magnitude, which decides whether a 20-inverter winding needs a 500 µF or a 5,000 µF class insulation monitor [1].

## 3. The physics: displacement current

For a capacitance C under a time-varying voltage V, the ideal displacement current is (standard physics):

```
I = C · dV/dt
```

If the capacitance also changes with time, as it does when an array wets or dries, the general form is:

```
I = d(CV)/dt = C · dV/dt + V · dC/dt
```

For a sinusoidal voltage of r.m.s. value V at angular frequency ω = 2πf, the r.m.s. current is I = ωCV.

The modules generate DC, but in a transformerless inverter part of the AC voltage reaches the array: in SMA's words, "the complete PV array oscillates with an alternating voltage in relation to its environment" [1, 3]. That common-mode swing charges and discharges the array capacitance. With the same waveform, more capacitance means more leakage [2]. Two figures show the scale:

- Single-phase 230 V, 8 µF array: 577 mA (Despotović and Vuković 2024, cited in [1]); check: 2π × 50 × 8 µF × 230 V ≈ 0.58 A.
- An AC-side first fault on an 800 V IT network impresses about the phase voltage (462 V) at 50 Hz on every floating array, driving **about 0.145 A per µF** through each inverter: 1.4 A at 10 µF, 14 A at 100 µF [1]. **Reasonably inferred, not measured.**

## 4. Concern: residual-current monitors and nuisance trips

Each transformerless inverter's residual-current monitoring unit (RCMU) sees only the vector sum of current at its own terminals [1].

- IEC 62109-2, as summarised in the research (background): trip on 300 mA continuous (10 mA/kVA above 30 kVA) and on 30, 60 and 150 mA steps within 300, 150 and 40 ms [1].
- SMA's string inverters, on an earthed star point, disconnect on "residual current jumps of > 30 mA", cap array coupling capacitance at 32 µF (Sunny Highpower) and 6 µF (Sunny Tripower X), and specify any external RCD as Type B at 1,000 to 1,500 mA. **Confirmed**; the link to wet-array leakage is **reasonably inferred** [1].
- BS 7671 411.6.3 notes that RCDs may trip on a first fault from capacitive leakage (**confirmed**); 411.6.5 requires an RCD in IT to have a non-operating residual current at least the first-fault current (existence confirmed, content background) [1].

**The owner's concern:** residual current alone does not distinguish healthy capacitive leakage from an insulation fault, and wet-weather leakage can push towards the thresholds and cause nuisance trips [2]. The research found **no product standard that tests RCMU or Riso performance when the AC side is IT with large network capacitance**: the inverters are certified on an assumed network, and an IT network carrying 14 arrays' capacitance is a different one [1].

## 5. Concern: first-fault capacitive currents when wet

On an IT network, a first earth fault has no metallic return path. The only current is capacitive, and the healthy conductors rise from 462 V to 800 V to earth. The research classes this mechanism as **confirmed** [1]. Its executive summary gives the first-fault current as:

| Condition | First-fault current (800 V winding) |
|---|---|
| Inverters off (cables and filters only) | about 1 A (model: 1.3 A) |
| Inverters on, arrays dry | about 4 A (model: 3.7 A) |
| Inverters on, front glass wet | model: 76 A |
| Inverters on, both faces wet | model: 149 A |

The summary rounds the wet cases to "75 to 150 A", explaining that the displacement reaches the module capacitance through the bridges. The 1,000 V figures are 1.2, 3.3, 67 and 133 A [1].

These are outputs of the research's **illustrative model**, "not a commissioned protection study": every input is a stated assumption (14 inverters of 475 kWp, SMA's nF/kW range, a 0.5 Ω earth grid, balanced steady-state phasors, RCMU trips not modelled dynamically). The owner's notes add that capacitive first-fault currents "cannot be assumed from the wet/dry ratio alone" [2]. What the model suggests [1]:

- **Touch voltage.** Local touch voltage on bonded steel stays under 30 V, but against remote earth through the 0.5 Ω grid it reaches 38 V with wet fronts and 75 V with both faces wet, above the 50 V condition of BS 7671 411.6.2 (R_A × I_d ≤ 50 V).
- **Lost generation.** Each inverter passes 5 to 10 A of residual current and trips within 40 to 300 ms, removing the array capacitance. A wet-day AC first fault therefore disconnects the whole winding's generation, the continuity IT was meant to preserve.
- **Self-masking** (**reasonably inferred**): inverters trip together, each passes its own pre-connection test, reconnects and trips again, none reports an AC fault; once all are off the current stops and the fault is invisible unless a winding IMD exists.

## 6. Concern: IMD configuration with large network capacitance

The insulation monitoring device (IMD) on the LV winding is the only device that watches the AC collection network while the plant generates, and it sees every array and feeder in parallel as one aggregate [1].

**Response time and range** (from manufacturer documents, as recorded in [1]): response is published numerically only at 1 µF (about 10 s typical, Bender isoPV1685DP; up to 90 s, isoPV485; Schneider IM20's 160 s filter "for photovoltaic applications"). Response at 500 to 4,000 µF is **unverified**. At 2,000 µF the isoPV1685's measuring range falls to 50 kΩ.

**Thresholds compared with general IT practice:**

| Setting basis | Alarm / pre-warning on 800 V |
|---|---|
| General IT practice, 100 Ω/V alarm, 300 Ω/V pre-warning (DIN VDE 0100-530) | 80 kΩ / 240 kΩ |
| PV IMD factory defaults | 1 kΩ / 10 kΩ |
| IEC 62548-1 above 500 kW | 1 kΩ accepted |

PV networks thus run about two orders of magnitude below other IT practice; Bender says thresholds are set on site after comparison measurements, so there is no published PV recommendation [1].

**Wet aggregation masks a real fault** (**reasonably inferred**, [1]). Twenty wet arrays at 50 kΩ each read as 2.5 kΩ, indistinguishable from one dangerous array at 2 kΩ in a dry network. The research suggests this is how recurring morning alarms come to be acknowledged without investigation, or thresholds raised.

**Other points** [1]: only one IMD per galvanically connected system; with more, Bender says "the device does not work and does not signal insulation faults" (**confirmed**, Schneider likewise). The isoPV1685DP's latching fault memory is off by default, so the BS 7671 requirement that the alarm persist depends on a commissioning setting (clause number 411.6.3.1 **disputed**, substance confirmed). Whether an insulation fault location system's current passes through an operating inverter is **unverified**.

## 7. Capacitive leakage compared with genuine insulation faults

This is the core of the owner's concern. A scheme designed for dry conditions may fail in either direction when the arrays are wet:

| | Healthy capacitive leakage | Genuine insulation fault |
|---|---|---|
| Cause | Water film raises cell-to-earth capacitance | Moisture reaches a damaged cable, connector or seal; insulation degrades |
| Nature | Reactive; depends on dV/dt and C | Resistive; conductive path to earth |
| Seen by RCMU | Yes, as residual current | Yes, if the current returns through that inverter; on a floating network it may stay below 300 mA [1] |
| Seen by IMD | In principle no, because the IMD measures the resistive response. But large C slows the measurement and limits its range | Yes, but masked in the parallel aggregate |
| Clears when dry | Yes | Not necessarily; it may recur |
| Risk if misread | Nuisance trips lead to raised thresholds | A real fault is acknowledged as "just the morning dew" |

The notes warn that moisture ingress "must not be confused with healthy capacitive leakage" [2], and the research proposes asking each inverter maker how an operator distinguishes a capacitive trip from an AC-side fault from a DC-side fault [1]. The notes add two limits [2]: an IMD may not detect a purely **series** high-resistance connector or series arc; and wet-panel capacitance can alter transient coupling but **does not automatically** mean a higher induced lightning voltage, which depends on geometry, bonding, impedance and SPD coordination (see 03).

## 8. What the owner proposes to check

From [1] and [2]:

1. **Assess wet as well as dry.** The earthing study states the network capacitance used for 411.6.2, wet and dry, and how it was obtained.
2. **Ask the inverter maker** for RCMU limits, adjustability, and the array capacitance up to which its protective functions are validated.
3. **One IMD per winding**, with stated capacitance range, justified thresholds, response time at measured capacitance, latching on, and a resistor functional test at commissioning.
4. **Trend, don't just alarm.** Correlate IMD readings and RCMU logs with weather: a decline that recovers on drying suggests leakage; one that persists or worsens suggests a genuine fault.
5. **Do not raise thresholds to silence wet-morning alarms** before finding the cause.

## References

1. Owner's independent research, *Earthing, inverter topology and protection in utility-scale solar*, 21 October 2026 (anonymised); citing SMA, Despotović and Vuković 2024, Bender, Schneider and BS 7671 411.6.
2. Owner's notes, *Wet weather, solar panel capacitance and lightning*, 22 October 2026; citing the Schneider Electric *Electrical Installation Guide* and SMA technical information.
3. SMA, *Technical Information: Leakage currents*, cited via [1] and [2]; not re-checked here.
4. DEHN, White Paper WPX051/EN/0422, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*: https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*
