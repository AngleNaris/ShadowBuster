import sys
from pathlib import Path
import numpy as np
import pytest
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apollo_scripts'))
import soundstage_reshape as width

SR = 44100

def pack(m, s):
    return np.column_stack((m+s, m-s))

def tones(freq=3000, ratio=.3, seconds=2):
    t=np.arange(int(SR*seconds))/SR
    m=.1*np.sin(2*np.pi*freq*t)
    s=.1*ratio*np.cos(2*np.pi*freq*t)
    return pack(m,s), pack(np.zeros_like(s), s*3)

def energy(x):
    return np.mean(x[8192:-8192]**2)

def test_mid_invariant_and_limited_high_growth():
    mix,delta=tones()
    accepted,report=width.constrain_width_delta(mix,delta,SR)
    out=mix+accepted
    np.testing.assert_allclose(out.mean(axis=1),mix.mean(axis=1),atol=1e-16)
    before=(mix[:,0]-mix[:,1])/2
    after=(out[:,0]-out[:,1])/2
    change=10*np.log10(energy(after)/energy(before))
    assert 0.2 < change <= 3.05
    assert report['bands']

def test_overwide_does_not_grow():
    mix,delta=tones(ratio=2)
    accepted,_=width.constrain_width_delta(mix,delta,SR)
    assert np.max(abs(accepted[8192:-8192])) < 1e-7

@pytest.mark.parametrize('freq',[40,60,90])
def test_low_frequency_width_is_not_added(freq):
    mix,delta=tones(freq=freq)
    accepted,_=width.constrain_width_delta(mix,delta,SR)
    assert energy(accepted) < 1e-5*energy(delta)

def test_zero_delta_and_short_inputs_are_identity():
    for n in [0,1,15,32,128,8192]:
        mix=np.random.default_rng(1).normal(0,.1,(n,2))
        out,_=width.constrain_width_delta(mix,np.zeros_like(mix),SR)
        np.testing.assert_array_equal(out,np.zeros_like(mix))
        out,_=width.constrain_width_delta(mix,mix*.1,SR)
        assert out.shape==mix.shape and np.isfinite(out).all()

def test_requested_width_is_monotonic_until_budget():
    mix,delta=tones()
    values=[]
    for wet in [0,.1,.3,.6,1]:
        added,_=width.constrain_width_delta(mix,delta*wet,SR)
        out=mix+added
        values.append(energy((out[:,0]-out[:,1])/2))
    assert np.all(np.diff(values)>=-1e-12)

def test_drums_attack_protected_more_than_tail():
    t=np.arange(SR)/SR
    env=np.where(t<.2,0,np.exp(-np.maximum(t-.2,0)/.15))
    stem=pack(np.sin(2*np.pi*1000*t)*env,np.sin(2*np.pi*3000*t)*env*.3)
    g=width.drum_width_envelope(stem,SR)
    assert np.mean(g[int(.205*SR):int(.22*SR)]) < np.mean(g[int(.3*SR):int(.4*SR)])
    assert np.min(g)>=.25-1e-9 and np.max(g)<=1+1e-9

def test_original_width_changes_by_section():
    narrow,d1=tones(ratio=.2,seconds=1)
    wide,d2=tones(ratio=2,seconds=1)
    mix=np.concatenate([narrow,wide]); delta=np.concatenate([d1,d2])
    added,_=width.constrain_width_delta(mix,delta,SR)
    assert np.mean(added[8192:SR-8192]**2)>1e-7
    assert np.mean(added[SR+8192:-8192]**2)<1e-12
