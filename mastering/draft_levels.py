"""Bounded-memory full-song K-weighted loudness calibration for live audition."""
import json
import sys
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal
from . import LOUDNESS_TARGETS


def coefficients(sr):
    # RBJ K-weighting, matching the default pyloudnorm meter used by final exports.
    A=10**(4/40);w=2*np.pi*1500/sr;c=np.cos(w);alpha=np.sin(w)/(2/np.sqrt(2))
    beta=2*np.sqrt(A)*alpha
    b=np.array([A*(A+1+(A-1)*c+beta),-2*A*(A-1+(A+1)*c),A*(A+1+(A-1)*c-beta)])
    a=np.array([A+1-(A-1)*c+beta,2*(A-1-(A+1)*c),A+1-(A-1)*c-beta])
    shelf=(b/a[0],a/a[0])
    w=2*np.pi*38/sr;c=np.cos(w);alpha=np.sin(w)
    b=np.array([(1+c)/2,-(1+c),(1+c)/2]);a=np.array([1+alpha,-2*c,1-alpha])
    high=(b/a[0],a/a[0])
    return [(b.tolist(), a.tolist()) for b,a in (shelf,high)]


def measure(path):
    sr = sf.info(path).samplerate
    coeff = coefficients(sr)
    states = [np.zeros((2, 2)), np.zeros((2, 2))]
    history, powers = [], []
    for block in sf.blocks(path, blocksize=round(sr*.1), dtype='float64', always_2d=True):
        for i, (b, a) in enumerate(coeff):
            block, states[i] = signal.lfilter(b, a, block, axis=0, zi=states[i])
        history.append(float(np.mean(np.sum(block*block, axis=1))))
        if len(history)>4:
            history.pop(0)
        if len(history)==4:
            powers.append(sum(history)/4)
    powers = np.asarray(powers or history)
    powers = powers[powers > 10**((-70+.691)/10)]
    if not len(powers):
        return -70.0
    powers = powers[powers >= np.mean(powers)/10]
    return float(-.691+10*np.log10(np.mean(powers)))


if __name__ == '__main__':
    Path(sys.argv[3]).write_text(json.dumps({
        'originalLufs': measure(sys.argv[1]), 'mixLufs': measure(sys.argv[2]),
        'targets': LOUDNESS_TARGETS, 'weighting': coefficients(44100),
    }), encoding='utf-8')
