"""Windows-часть с поддельным COM: настоящие таблицы методов, настоящие указатели, подсчёт ссылок."""
import ctypes, os, sys, tempfile, uuid, json
from ctypes import *
root = tempfile.mkdtemp(); os.environ.update(GOLOS_DATA=root, GOLOS_NO_BROWSER="1", GOLOS_NO_TRAY="1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths; paths.use()
ctypes.WINFUNCTYPE = CFUNCTYPE

class World:
    devices = [("{mic}", "Микрофон (Realtek(R) Audio)"), ("{cable}", "CABLE Output (VB-Audio Virtual Cable)")]
    defaults = {0: "{mic}", 1: "{mic}", 2: "{mic}"}
    refs = {}; keep = []; freed = 0; allocated = 0; cleared = 0; inited = 0
W = World

def obj(methods):
    """methods: {индекс: (argtypes, функция)}. Индексы 0-2 IUnknown."""
    table = (c_void_p * 20)()
    me = (c_void_p * 1)(addressof(table))
    addr = addressof(me)
    def release(this):
        W.refs[addr] -= 1; return W.refs[addr]
    all_m = {2: ((), release), **methods}
    for i, (args, fn) in all_m.items():
        cb = CFUNCTYPE(c_long, c_void_p, *args)(fn)
        table[i] = cast(cb, c_void_p).value
        W.keep.append(cb)
    W.keep += [table, me]; W.refs[addr] = 1
    return addr

def out(ptr, value):
    cast(ptr, POINTER(c_void_p))[0] = value

def wstr(text):
    b = create_unicode_buffer(text); W.keep.append(b); W.allocated += 1; return addressof(b)

class PV(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("value", c_void_p), ("pad", c_void_p)]

def make_store(name):
    def get_value(this, key, pv):
        raw = string_at(key, 20)
        assert uuid.UUID(bytes_le=raw[:16]) == uuid.UUID("A45C254E-DF1C-4EFD-8020-67D146A850E0")
        assert int.from_bytes(raw[16:], "little") == 14
        p = cast(pv, POINTER(PV)).contents
        p.vt = 31; p.value = wstr(name); return 0
    return obj({5: ((c_void_p, c_void_p), get_value)})

def make_device(dev):
    def store(this, access, out_p):
        assert access == 0; out(out_p, make_store(dev[1])); return 0
    def get_id(this, out_p):
        out(out_p, wstr(dev[0])); return 0
    return obj({4: ((c_uint32, c_void_p), store), 5: ((c_void_p,), get_id)})

def make_collection():
    def count(this, p):
        cast(p, POINTER(c_uint32))[0] = len(W.devices); return 0
    def item(this, i, p):
        out(p, make_device(W.devices[i])); return 0
    return obj({3: ((c_void_p,), count), 4: ((c_uint32, c_void_p), item)})

def make_enum():
    def enum(this, flow, state, p):
        assert flow == 1 and state == 1; out(p, make_collection()); return 0
    def default(this, flow, role, p):
        assert flow == 1
        d = next(x for x in W.devices if x[0] == W.defaults[role]); out(p, make_device(d)); return 0
    return obj({3: ((c_uint32, c_uint32, c_void_p), enum), 4: ((c_uint32, c_uint32, c_void_p), default)})

def make_policy():
    def set_default(this, device, role):
        W.defaults[role] = wstring_at(device); return 0
    return obj({13: ((c_void_p, c_uint32), set_default)})

class F:
    def __init__(self, fn): self.fn = fn; self.argtypes = None; self.restype = None
    def __call__(self, *a):
        return self.fn(*a)

def guid_of(p):
    return uuid.UUID(bytes_le=string_at(p, 16))

def cocreate(clsid, outer, ctx, iid, out_p):
    assert ctx == 23
    c = str(guid_of(clsid)).upper(); i = str(guid_of(iid)).upper()
    if c == "BCDE0395-E52F-467C-8E3D-C4579291692E":
        assert i == "A95664D2-9614-4F35-A746-DE8DB63617E6"; out_p._obj.value = make_enum(); return 0
    if c == "870AF99C-171D-4F9E-AF0D-E63DF40C2BC9":
        assert i == "F8679F50-850A-41CF-9C72-430F290290C8"; out_p._obj.value = make_policy(); return 0
    return -2147221164

class Ole:
    def __init__(self):
        def init(a, b): W.inited += 1; return 0
        def uninit(): W.inited -= 1
        def free(p): W.freed += 1
        def clear(p): W.cleared += 1; return 0
        self.CoInitializeEx = F(init); self.CoUninitialize = F(uninit); self.CoTaskMemFree = F(free)
        self.PropVariantClear = F(clear); self.CoCreateInstance = F(cocreate)

def windll(name, **kw):
    assert name == "ole32", name
    return Ole()
ctypes.WinDLL = windll

class Ref:
    pass
import winsys
# byref в настоящем ctypes не даёт питоновского объекта; для подделки передаём его через обёртку
orig_byref = ctypes.byref
class CCArg:
    def __init__(self, o): self._obj = o; self.addr = addressof(o)
winsys_byref = lambda o: orig_byref(o)
def patched_cocreate(self, clsid, iid):
    res = c_void_p(); a, b = winsys._guid(clsid), winsys._guid(iid)
    r = cocreate(addressof(a), None, 23, addressof(b), CCArg(res))
    winsys._check(r, "x"); return int(res.value or 0)
winsys._Com.create = patched_cocreate

_orig_method = winsys._method
print("устройства:", winsys.capture_devices())
assert winsys.capture_devices()[1] == ("{cable}", "CABLE Output (VB-Audio Virtual Cable)")
print("основные:", winsys.default_capture_ids())
print("кабель:", winsys.find_cable_capture())
saved = winsys.make_cable_default(); print("сохранено", saved, "теперь", W.defaults)
assert set(W.defaults.values()) == {"{cable}"} and set(saved.values()) == {"{mic}"}
winsys.restore_default(saved); assert set(W.defaults.values()) == {"{mic}"}; print("вернули", W.defaults)
leaks = {k: v for k, v in W.refs.items() if v != 0}
print("утечки объектов:", len(leaks), "| строк выделено", W.allocated, "освобождено", W.freed, "| PropVariantClear", W.cleared, "| незакрытых CoInitialize", W.inited)
assert not leaks and W.inited == 0

print("-- сценарий программы")
import app as A
a = A.App(); a.scan_devices()
a.do({"action": "set_all_apps", "value": True}); print(a.settings["all_apps"], a.settings["restore_mic"], W.defaults, repr(a.all_apps_error))
assert a.settings["all_apps"] and set(W.defaults.values()) == {"{cable}"} and a.settings["restore_mic"]["0"] == "{mic}"
a.save()
print("-- аварийный выход: программа перезапускается с уже подставленным кабелем")
b = A.App(); b.scan_devices(); b.recover_default_mic()
print(b.settings["all_apps"], b.settings["restore_mic"], W.defaults)
assert b.settings["restore_mic"]["0"] == "{mic}"
b.do({"action": "set_all_apps", "value": False}); assert set(W.defaults.values()) == {"{mic}"} and b.settings["restore_mic"] is None
print("выключили:", W.defaults)
print("-- аварийный выход с выключенным режимом, но записанным микрофоном")
W.defaults.update({0: "{cable}", 1: "{cable}", 2: "{cable}"})
b.settings.update(all_apps=False, restore_mic={"0": "{mic}", "1": "{mic}", "2": "{mic}"}); b.recover_default_mic()
assert set(W.defaults.values()) == {"{mic}"} and b.settings["restore_mic"] is None; print("вернули при запуске:", W.defaults)
print("-- обычный выход возвращает микрофон")
c = A.App(); c.scan_devices(); c.do({"action": "set_all_apps", "value": True}); assert set(W.defaults.values()) == {"{cable}"}
c.hotkeys = type("H", (), {"stop": lambda s: None})(); c.quit()
assert set(W.defaults.values()) == {"{mic}"}
saved_s = json.loads(A.SETTINGS_PATH.read_text(encoding="utf-8")); print("после выхода:", W.defaults, "режим запомнен:", saved_s["all_apps"], "restore:", saved_s["restore_mic"])
assert saved_s["all_apps"] and saved_s["restore_mic"] is None
print("ОК"); os._exit(0)
