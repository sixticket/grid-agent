"""Bounded on-disk cache of exact optimal trial dispatches, scoped to one scenario."""
import copy,hashlib,json,tempfile,time
from collections import OrderedDict
from pathlib import Path
import numpy as np
import pypsa

class TrialDispatchCache:
    def __init__(self,limit=2):
        self.directory=tempfile.TemporaryDirectory(prefix='grid-trial-dispatch-')
        self.entries=OrderedDict();self.limit=limit
    def put(self,key,result,network):
        if result.get('status')!='optimal' or network is None:return
        path=Path(self.directory.name)/(key+'.nc')
        def encode(value):
            if isinstance(value,np.ndarray):return {'__trial_array__':value.tolist(),'dtype':str(value.dtype)}
            if isinstance(value,np.generic):return value.item()
            raise TypeError(type(value).__name__)
        old=network.meta
        try:
            network.meta=json.loads(json.dumps(old,default=encode))
            network.export_to_netcdf(path)
        finally:network.meta=old
        self.entries[key]=(copy.deepcopy(result),path,hashlib.sha256(path.read_bytes()).hexdigest())
        self.entries.move_to_end(key)
        while len(self.entries)>self.limit:
            _,(_,expired,_)=self.entries.popitem(last=False);expired.unlink()
    def get(self,key):
        entry=self.entries.get(key)
        if entry is None:return None
        start=time.monotonic();result,path,digest=entry
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Trial dispatch checksum mismatch')
        network=pypsa.Network(path)
        def decode(value):
            if '__trial_array__' in value:return np.array(value['__trial_array__'],dtype=value['dtype'])
            return value
        network.meta=json.loads(json.dumps(network.meta),object_hook=decode)
        self.entries.move_to_end(key);result=copy.deepcopy(result)
        result['dispatch_reuse']=dict(hit=True,key=key,dispatch_sha256=digest,original_solve_seconds=result.get('solve_seconds',0),load_seconds=round(time.monotonic()-start,3))
        result['solve_seconds']=0.0
        return result,network
