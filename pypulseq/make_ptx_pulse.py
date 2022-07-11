from types import SimpleNamespace

from pypulseq.opts import Opts


def make_ptx_pulse(flip_angle: float, rf_type: str = 'exc', delay: float = 0, duration: float = 0, freq_offset: float = 0, phase_offset: float = 0) -> SimpleNamespace:
    """
    Creates a pTx dummy pulse for the pTx extension.
    WIP: Maybe add parameters: pulse ID?, selective/non-selective?

    Parameters
    ----------
    flip_angle: Flip angle [deg]
    rf_type: str, default='exc'
        Pulse type. Currently only excitation ('exc') or refocusing ('ref')
    delay : float, optional, default=0
        Delay, [s].
    duration : float, optional, default=0
        Duration of pTx pulse [s]
    system : Opts, optional, default=Opts()
        System limits.

    Returns
    ------
    pulse : SimpleNamespace
        Dummy pTx pulse event.

    Raises
    ------
    ValueError
        If `channel` is invalid. Must be one of 'osc0','osc1', or 'ext1'.
    """
    if rf_type not in ['exc', 'ref']:
        raise ValueError(f"RF type {rf_type} is invalid. Must be one of 'exc' or 'ref'.")

    rf_types = {'exc': 1, 'ref': 2}

    pulse = SimpleNamespace()
    pulse.type = 'ptx'
    pulse.rf_type = rf_types[rf_type]
    pulse.flip_angle = flip_angle
    pulse.delay = delay
    pulse.duration = duration
    pulse.freq_offset = freq_offset
    pulse.phase_offset = phase_offset

    return pulse
