from types import SimpleNamespace

from pypulseq.opts import Opts


def make_ptx_pulse(rf_type: str = 'exc', delay: float = 0, duration: float = 0) -> SimpleNamespace:
    """
    Creates a pTx dummy pulse for the pTx extension.
    WIP: Maybe add parameters: pulse ID?, selective/non-selective?

    Parameters
    ----------
    type: str, default='exc'
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
    pulse.delay = delay
    pulse.duration = duration

    return pulse
