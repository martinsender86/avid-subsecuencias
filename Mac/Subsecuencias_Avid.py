# -*- coding: utf-8 -*-
"""
Subsecuencias Avid  v1.2.4
Crea una subsecuencia por cada corte de una secuencia, dentro del mismo bin.

Se suelta el .avb en la ventana (o sobre el .exe), se elige la secuencia y la
pista que define los cortes, y el programa agrega al bin una secuencia por
plano, con todas las pistas recortadas a ese tramo (video, audio, TC,
efectos, keyframes y locators).

Con el segundo botón, las subsecuencias elegidas se convierten en subclips
(como hacer AutoSync en Avid): cada pista apunta al mismo clip que tenía la
subsecuencia (master clip o subclip).

Avid puede quedar abierto: lo único que hay que cerrar es ESE bin.

Uso por línea de comandos (pruebas):
  python Subsecuencias_Avid.py --cli BIN.avb [--secuencia NOMBRE] [--pistas V1]
                               [--simular] [--con-huecos] [--sin-unir-add-edits]
                               [--nombre "{secuencia}_{n}"]
  python Subsecuencias_Avid.py --cli BIN.avb --subclips --secuencia NOMBRE ...
                               [--conservar] [--simular]
"""
from __future__ import print_function

import os
import sys
import math
import shutil
import datetime
import tempfile
import traceback
from fractions import Fraction

try:
    import avb
    import avb.mobid
    import avb.trackgroups
except ImportError:  # pragma: no cover
    avb = None

VERSION = "1.2.4"
NOMBRE_DEFECTO = "{secuencia}_{n}"
CARPETA_BACKUP = "_Backup_Subsecuencias"
SUFIJO_CONSERVAR = ".sync.01"   # nombre del subclip si se conservan las subsecuencias

# class_id de los componentes que importan
FILL, SCLP, SEQU, TNFX = b'FILL', b'SCLP', b'SEQU', b'TNFX'
TCCP, ECCP = b'TCCP', b'ECCP'
CP_INT, CP_DOUBLE = 1, 2      # tipos de valor de los keyframes (ParamClip)


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def cid(obj):
    try:
        return bytes(obj.class_id)
    except Exception:
        return b''


def es_timewarp(obj):
    return isinstance(obj, avb.trackgroups.TimeWarp)


def es_trackgroup(obj):
    return isinstance(obj, avb.trackgroups.TrackGroup) and not isinstance(
        obj, avb.trackgroups.Composition)


def comp_de(track):
    try:
        return track.component
    except AttributeError:
        return None


def etiqueta_pista(track):
    c = comp_de(track)
    kind = c.media_kind if c is not None else ''
    letra = {'picture': 'V', 'sound': 'A'}.get(kind)
    if not letra:
        return None
    return "%s%d" % (letra, track.index)


def pistas_av(mob):
    """[(etiqueta, track)] de las pistas de video y audio, V primero."""
    res = []
    for t in mob.tracks:
        lab = etiqueta_pista(t)
        if lab:
            res.append((lab, t))
    res.sort(key=lambda x: (x[0][0] != 'V', int(x[0][1:])))
    return res


def secuencias_del_bin(f):
    res = []
    for mob in f.content.mobs:
        try:
            if mob.mob_type == 'CompositionMob' and mob.usage_code == 0:
                res.append(mob)
        except Exception:
            pass
    return res


def tc_de_secuencia(mob):
    """(inicio en frames, fps nominal, drop) de la pista de TC principal."""
    mejor = None
    for t in mob.tracks:
        c = comp_de(t)
        if c is not None and cid(c) == TCCP:
            if mejor is None or t.index < mejor[0]:
                mejor = (t.index, c)
    if mejor is None:
        return 0, 24, False
    c = mejor[1]
    return c.start, c.fps or 24, bool(c.flags & 1)


def frames_a_tc(frames, fps, drop=False):
    fps = int(fps) or 24
    if drop and fps in (30, 60):
        d = 2 if fps == 30 else 4
        por10 = fps * 600 - d * 9
        por1 = fps * 60 - d
        dec, resto = divmod(frames, por10)
        if resto > d:
            frames += d * 9 * dec + d * ((resto - d) // por1)
        else:
            frames += d * 9 * dec
    ff = frames % fps
    s = frames // fps
    sep = ';' if drop else ':'
    return "%02d:%02d:%02d%s%02d" % (s // 3600 % 24, s // 60 % 60, s % 60, sep, ff)


def nombre_clip(f, seg):
    """Nombre del primer clip que encuentre dentro del segmento."""
    pila = [seg]
    vistos = 0
    while pila and vistos < 200:
        c = pila.pop(0)
        vistos += 1
        k = cid(c)
        if k == SCLP:
            try:
                m = f.content.find_by_mob_id(c.mob_id)
                if m is not None and m.name:
                    return m.name
            except Exception:
                pass
        elif k == SEQU:
            pila.extend(list(c.components))
        elif hasattr(c, 'tracks') and not callable(getattr(c, 'tracks')):
            if cid(c) == b'SLCT':
                try:
                    pila.append(c.tracks[c.selected].component)
                    continue
                except Exception:
                    pass
            for t in c.tracks:
                cc = comp_de(t)
                if cc is not None:
                    pila.append(cc)
    return ""


# ---------------------------------------------------------------------------
# modelo plano de una secuencia (posiciones absolutas, transiciones)
# ---------------------------------------------------------------------------

class Seg(object):
    __slots__ = ('obj', 's', 'e', 'pos')

    def __init__(self, obj, pos):
        self.obj = obj
        self.pos = pos            # posición original del segmento
        self.s = pos              # inicio efectivo
        self.e = pos + obj.length  # fin efectivo


class Tra(object):
    __slots__ = ('obj', 'ts', 'te', 'c', 'antes', 'despues')

    def __init__(self, obj, ts):
        self.obj = obj
        self.ts = ts
        self.te = ts + obj.length
        cp = getattr(obj, 'cutpoint', 0) or 0
        self.c = ts + max(0, min(cp, obj.length))
        self.antes = None
        self.despues = None


def plano(comp):
    """Devuelve (lista ordenada de Seg/Tra) de un componente de pista."""
    if cid(comp) != SEQU:
        return [Seg(comp, 0)]
    items = []
    pos = 0
    ultimo_seg = None
    pendiente = None
    for c in comp.components:
        if cid(c) == TNFX:
            pos -= c.length
            t = Tra(c, pos)
            t.antes = ultimo_seg
            items.append(t)
            pendiente = t
        else:
            s = Seg(c, pos)
            items.append(s)
            if pendiente is not None:
                pendiente.despues = s
                pendiente = None
            ultimo_seg = s
            pos += c.length
    return items


def romper_transiciones(items, a=None, b=None):
    """Convierte en corte (en su cut point) toda transición que no quede
    entera adentro de [a, b). Sin a/b, rompe todas. Devuelve las que quedan."""
    quedan = set()
    for it in items:
        if not isinstance(it, Tra):
            continue
        if a is not None and a < it.ts and it.te < b and it.antes and it.despues:
            quedan.add(id(it))
            continue
        if it.antes is not None:
            it.antes.e = min(it.antes.e, it.c)
        if it.despues is not None:
            it.despues.s = max(it.despues.s, it.c)
    return quedan


# ---------------------------------------------------------------------------
# recorte de componentes
# ---------------------------------------------------------------------------

class Recortador(object):
    def __init__(self, f):
        self.f = f
        self.avisos = []

    def aviso(self, txt):
        if txt not in self.avisos:
            self.avisos.append(txt)

    # -- creación --------------------------------------------------------
    def relleno(self, modelo, n):
        fil = self.f.create.Filler(edit_rate=modelo.edit_rate,
                                   media_kind=modelo.media_kind)
        fil.length = n
        return fil

    # -- secuencias --------------------------------------------------------
    def cortar_secuencia(self, comp, a, b):
        items = plano(comp)
        quedan = romper_transiciones(items, a, b)
        nuevos = [self.relleno(comp, 0)]
        for it in items:
            if isinstance(it, Tra):
                if id(it) in quedan:
                    nuevos.append(it.obj.copy(self.f))
                continue
            s = max(it.s, a)
            e = min(it.e, b)
            if e <= s:
                continue
            nuevos.append(self.recortar(it.obj, s - it.pos, e - s))
        nuevos.append(self.relleno(comp, 0))

        seq = self.f.create.Sequence(edit_rate=comp.edit_rate,
                                     media_kind=comp.media_kind)
        for c in nuevos:
            seq.components.append(c)
        largo = seq.length
        if largo != b - a:
            if largo < b - a:
                # completa con relleno (pista más corta que el tramo)
                seq.components.insert(len(seq.components) - 1,
                                      self.relleno(comp, (b - a) - largo))
            else:
                self.aviso("Una pista quedó %d frames más larga de lo esperado."
                           % (largo - (b - a)))
        return seq

    # -- cualquier componente ----------------------------------------------
    def recortar(self, obj, off, n):
        """Copia de obj que cubre [off, off+n) de su propio tiempo."""
        if off == 0 and n == obj.length:
            return obj.copy(self.f)
        k = cid(obj)

        if k == SEQU:
            return self.cortar_secuencia(obj, off, off + n)

        if k == FILL:
            nuevo = obj.copy(self.f)
            nuevo.length = n
            return nuevo

        if k == SCLP:
            nuevo = obj.copy(self.f)
            nuevo.start_time = obj.start_time + off
            nuevo.length = n
            self.locators(nuevo, off, n)
            return nuevo

        if k == TCCP:
            nuevo = obj.copy(self.f)
            nuevo.start = obj.start + off
            nuevo.length = n
            return nuevo

        if k == ECCP:
            nuevo = obj.copy(self.f)
            nuevo.start_ec = obj.start_ec + off
            nuevo.length = n
            return nuevo

        if k in (b'CTRL', b'PRCL'):
            # pista de automatización (volumen/paneo) o de parámetros
            nuevo = obj.copy(self.f)
            try:
                self.correr_puntos(nuevo, obj, off, n)
            except Exception as e:
                self.aviso("No se pudieron ajustar unos keyframes (%s): "
                           "revisalos en Avid." % e)
            nuevo.length = n
            return nuevo

        if es_timewarp(obj):
            return self.recortar_timewarp(obj, off, n)

        if es_trackgroup(obj):
            return self.recortar_grupo(obj, off, n)

        # tipo desconocido: se copia con el largo nuevo
        nuevo = obj.copy(self.f)
        try:
            nuevo.length = n
        except Exception:
            pass
        self.aviso("Se recortó un componente de tipo %s sin tratamiento "
                   "específico: revisalo en Avid." % type(obj).__name__)
        return nuevo

    def recortar_grupo(self, obj, off, n):
        """Efectos de pista, multicam/group (Selector), EssenceGroup, etc.:
        las entradas están alineadas con el efecto."""
        nuevo = obj.copy(self.f)
        nuevo.length = n
        for i, t in enumerate(obj.tracks):
            c = comp_de(t)
            if c is None:
                continue
            disp = c.length - off
            if disp <= 0:
                nuevo.tracks[i].component = self.relleno(c, n)
                continue
            nuevo.tracks[i].component = self.recortar(c, off, min(n, disp))
        self.keyframes(nuevo, obj, off, n)
        self.locators(nuevo, off, n)
        return nuevo

    def recortar_timewarp(self, obj, off, n):
        """Motion effect / capture mask / strobe / freeze."""
        nuevo = obj.copy(self.f)
        nuevo.length = n
        try:
            interior = obj.tracks[0].component
        except Exception:
            interior = None

        rampa = self.tiene_rampa(obj)
        nombre = type(obj).__name__

        if interior is None or nombre == 'Repeat' or rampa:
            # freeze o velocidad variable: la entrada queda entera y se
            # corren los keyframes
            self.keyframes(nuevo, obj, off, n)
            self.locators(nuevo, off, n)
            if rampa:
                self.aviso("Se partió un efecto de velocidad variable: "
                           "revisá que la rampa quedó bien.")
            return nuevo

        Lin, Lout = interior.length, obj.length
        rate = None
        sr = getattr(obj, 'speed_ratio', None)
        if sr and len(sr) == 2 and sr[1]:
            rate = Fraction(int(sr[0]), int(sr[1]))
        if rate is None or rate <= 0:
            rate = Fraction(Lin, Lout) if Lout else Fraction(1)

        k = int(math.floor(off * rate))
        extra = 1 if nombre == 'MotionEffect' else 0
        m = int(math.ceil(n * rate)) + extra
        m = max(1, min(m, Lin - k))
        if k >= Lin:
            k, m = max(0, Lin - 1), 1
        nuevo.tracks[0].component = self.recortar(interior, k, m)
        if nombre == 'MotionEffect' and hasattr(obj, 'phase_offset'):
            try:
                nuevo.phase_offset = obj.phase_offset + k
            except Exception:
                pass
        self.keyframes(nuevo, obj, off, n)
        self.locators(nuevo, off, n)
        return nuevo

    # -- keyframes -----------------------------------------------------------
    @staticmethod
    def listas_param(obj):
        res = []
        for nombre in ('param_list', 'source_param_list'):
            try:
                pl = getattr(obj, nombre)
            except AttributeError:
                continue
            if pl:
                res.append(pl)
        return res

    def tiene_rampa(self, obj):
        for pl in self.listas_param(obj):
            for p in pl:
                ct = getattr(p, 'control_track', None)
                pts = getattr(ct, 'control_points', None) if ct is not None else None
                if pts and len(pts) > 1:
                    return True
        return False

    def keyframes(self, nuevo, original, off, n):
        nuevas = self.listas_param(nuevo)
        viejas = self.listas_param(original)
        for pl_n, pl_o in zip(nuevas, viejas):
            for p_n, p_o in zip(pl_n, pl_o):
                ct_n = getattr(p_n, 'control_track', None)
                ct_o = getattr(p_o, 'control_track', None)
                if ct_n is None or ct_o is None:
                    continue
                try:
                    self.correr_puntos(ct_n, ct_o, off, n)
                except Exception as e:  # nunca frenar por un keyframe
                    self.aviso("No se pudieron ajustar unos keyframes (%s): "
                               "revisalos en Avid." % e)

    def correr_puntos(self, ct_n, ct_o, off, n):
        k = cid(ct_n)
        if k not in (b'PRCL', b'CTRL'):
            return
        try:
            ct_n.length = n
        except Exception:
            pass
        pts_n = list(ct_n.control_points)
        pts_o = list(ct_o.control_points)
        if not pts_n:
            return
        es_param = (k == b'PRCL')
        escala = 'timescale' if es_param else 'time_scale'

        def tiempo(cp):
            ts = getattr(cp, escala) or 1
            return Fraction(int(cp.offset[0]), int(cp.offset[1] or 1)) * ts

        def poner(cp, t):
            ts = getattr(cp, escala) or 1
            q = Fraction(t) / ts
            cp.offset = [q.numerator, q.denominator]

        def valor(t):
            if es_param:
                vt = ct_o.value_type
                if vt not in (CP_INT, CP_DOUBLE):   # referencia: no se interpola
                    return None
                v = ct_o.value_at(float(t))
                return int(round(v)) if vt == CP_INT else v
            # ControlClip: lineal sobre racionales
            ts_ = [tiempo(c) for c in pts_o]
            vs = [Fraction(int(c.value[0]), int(c.value[1] or 1)) for c in pts_o]
            if t <= ts_[0]:
                v = vs[0]
            elif t >= ts_[-1]:
                v = vs[-1]
            else:
                i = max(j for j in range(len(ts_)) if ts_[j] <= t)
                d = ts_[i + 1] - ts_[i]
                v = vs[i] + (vs[i + 1] - vs[i]) * ((t - ts_[i]) / d if d else 0)
            v = v.limit_denominator(1 << 20)
            return [v.numerator, v.denominator]

        if len(pts_n) == 1:
            # un solo punto = valor constante: se lo deja dentro del tramo
            cp = pts_n[0]
            poner(cp, min(max(tiempo(cp) - off, 0), n))
            return

        fin = off + n
        antes = [c for c in pts_n if tiempo(c) < off]
        dentro = [c for c in pts_n if off <= tiempo(c) <= fin]
        despues = [c for c in pts_n if tiempo(c) > fin]

        t_prim = tiempo(dentro[0]) if dentro else None
        t_ult = tiempo(dentro[-1]) if dentro else None
        res = []
        if antes and (not dentro or t_prim > off):
            cp = antes[-1]
            v = valor(off)
            if v is not None:
                cp.value = v
            poner(cp, 0)
            res.append(cp)
        for cp in dentro:
            poner(cp, tiempo(cp) - off)
            res.append(cp)
        if despues and (not dentro or t_ult < fin):
            cp = despues[0]
            v = valor(fin)
            if v is not None:
                cp.value = v
            poner(cp, n)
            res.append(cp)
        if not res:
            return
        ct_n.control_points = res

    # -- locators -------------------------------------------------------------
    def locators(self, obj, off, n):
        try:
            attrs = obj.attributes
        except AttributeError:
            return
        if not attrs or '_TMP_CRM' not in attrs:
            return
        lista = attrs['_TMP_CRM']
        for i in reversed(range(len(lista))):
            m = lista[i]
            co = getattr(m, 'comp_offset', 0) or 0
            if off <= co < off + n:
                m.comp_offset = co - off
            else:
                lista.pop(i)


# ---------------------------------------------------------------------------
# cortes
# ---------------------------------------------------------------------------

def es_relleno(obj):
    return cid(obj) == FILL


def mismo_material(a, b):
    """True si b sigue a a sin salto (add edit / match frame)."""
    if cid(a) != SCLP or cid(b) != SCLP:
        return False
    try:
        return (a.mob_id == b.mob_id and a.track_id == b.track_id
                and a.start_time + a.length == b.start_time)
    except Exception:
        return False


def buscar_cortes(mob, etiquetas, unir_add_edits=True, con_huecos=False):
    """Devuelve [(inicio, fin, seg_de_referencia)] en frames de la secuencia."""
    largo = mob.length
    pistas = dict(pistas_av(mob))
    usadas = [pistas[e] for e in etiquetas if e in pistas]
    if not usadas:
        raise ValueError("La secuencia no tiene las pistas %s." % ", ".join(etiquetas))

    bordes = set([0, largo])
    planos = []
    for t in usadas:
        items = plano(t.component)
        romper_transiciones(items)
        segs = [s for s in items if isinstance(s, Seg) and s.e > s.s]
        planos.append(segs)
        for i, s in enumerate(segs):
            for borde in (s.s, s.e):
                if 0 < borde < largo:
                    bordes.add(borde)

    # add edits: un borde se descarta si en todas las pistas de referencia
    # o no es borde, o es continuación del mismo material
    if unir_add_edits:
        descartar = set()
        for b in bordes:
            if b in (0, largo):
                continue
            es_add = True
            for segs in planos:
                izq = next((s for s in segs if s.e == b), None)
                der = next((s for s in segs if s.s == b), None)
                if izq is None and der is None:
                    continue           # en esta pista no hay corte ahí
                if izq is None or der is None:
                    es_add = False
                    break
                if not (mismo_material(izq.obj, der.obj)
                        and izq.e == izq.pos + izq.obj.length
                        and der.s == der.pos):
                    es_add = False
                    break
            if es_add:
                descartar.add(b)
        bordes -= descartar

    puntos = sorted(bordes)
    tramos = []
    for a, b in zip(puntos, puntos[1:]):
        ref = None
        for segs in planos:
            s = next((s for s in segs if s.s <= a < s.e), None)
            if s is not None and not es_relleno(s.obj):
                ref = s.obj
                break
        if ref is None and not con_huecos:
            continue
        tramos.append((a, b, ref))
    return tramos


# ---------------------------------------------------------------------------
# subsecuencias
# ---------------------------------------------------------------------------

def crear_subsecuencia(f, mob, a, b, nombre, rec):
    nuevo = mob.copy(f)
    nuevo.mob_id = avb.mobid.MobID.new()
    nuevo.name = nombre
    ahora = datetime.datetime.now()
    try:
        nuevo.creation_time = ahora
        nuevo.last_modified = ahora
    except Exception:
        pass
    n = b - a
    for i, t in enumerate(mob.tracks):
        c = comp_de(t)
        if c is None:
            continue
        if c.length <= a:
            nuevo.tracks[i].component = rec.relleno(c, n)
            continue
        disp = min(n, c.length - a)
        if cid(c) == SEQU:
            nuevo.tracks[i].component = rec.cortar_secuencia(c, a, a + n)
        else:
            nuevo.tracks[i].component = rec.recortar(c, a, disp)
    nuevo.length = n
    return nuevo


def raiz_proyecto(ruta_bin):
    d = os.path.dirname(os.path.abspath(ruta_bin))
    for _ in range(5):
        try:
            if any(x.lower().endswith('.avp') for x in os.listdir(d)):
                return d
        except OSError:
            pass
        padre = os.path.dirname(d)
        if padre == d:
            break
        d = padre
    return None


def carpeta_backup(ruta_bin):
    raiz = raiz_proyecto(ruta_bin)
    if raiz:
        return os.path.join(raiz, 'Trash', CARPETA_BACKUP)
    if sys.platform.startswith('win'):
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'AvidSubsecuencias', 'backups')
    return os.path.join(os.path.expanduser('~'), '.avid_subsecuencias', 'backups')


def armar_nombre(patron, secuencia, n, clip, tc):
    try:
        txt = patron.format(secuencia=secuencia, n="%03d" % n, clip=clip, tc=tc)
    except (KeyError, IndexError, ValueError):
        txt = "%s_%03d" % (secuencia, n)
    txt = " ".join(txt.split())
    return txt.strip(" _-") or "%s_%03d" % (secuencia, n)


def procesar(ruta_bin, nombres_secuencias=None, etiquetas=('V1',),
             patron=NOMBRE_DEFECTO, unir_add_edits=True, con_huecos=False,
             simular=False, log=print):
    """Crea las subsecuencias. Devuelve la lista de nombres creados."""
    if avb is None:
        raise RuntimeError("Falta la librería pyavb (pip install pyavb).")
    ruta_bin = os.path.abspath(ruta_bin)
    if not os.path.isfile(ruta_bin):
        raise IOError("No existe el bin: %s" % ruta_bin)

    mtime0 = os.path.getmtime(ruta_bin)
    f = avb.open(ruta_bin)
    total = 0
    tmp = None
    try:
        secuencias = secuencias_del_bin(f)
        if nombres_secuencias:
            elegidas = [s for s in secuencias if s.name in nombres_secuencias]
        else:
            elegidas = secuencias[:1]
        if not elegidas:
            raise ValueError("No encontré la secuencia en el bin.")

        existentes = set(m.name for m in f.content.mobs
                         if getattr(m, 'mob_type', '') == 'CompositionMob')
        rec = Recortador(f)
        nuevos = []
        for mob in elegidas:
            inicio, fps, drop = tc_de_secuencia(mob)
            tramos = buscar_cortes(mob, list(etiquetas), unir_add_edits, con_huecos)
            log("%s: %d planos (cortes en %s)" % (mob.name, len(tramos),
                                                  "+".join(etiquetas)))
            for i, (a, b, ref) in enumerate(tramos, 1):
                clip = nombre_clip(f, ref) if ref is not None else ""
                tc = frames_a_tc(inicio + a, fps, drop)
                nombre = armar_nombre(patron, mob.name, i, clip, tc)
                linea = "  %3d  %s  %5d fr  %s" % (i, tc, b - a, nombre)
                if nombre in existentes:
                    log(linea + "   (ya existe: se saltea)")
                    continue
                log(linea)
                if simular:
                    continue
                nuevos.append(crear_subsecuencia(f, mob, a, b, nombre, rec))
                existentes.add(nombre)

        for a in rec.avisos:
            log("AVISO: " + a)

        if simular:
            log("Simulación: no se escribió nada.")
            return []
        if not nuevos:
            log("No hay subsecuencias nuevas para crear.")
            return []

        for m in nuevos:
            f.content.add_mob(m)
        total = len(nuevos)
        nombres_nuevos = [m.name for m in nuevos]
        tmp = escribir_temporal(f, ruta_bin)
    finally:
        f.close()

    def chequeo(g):
        nombres = set(m.name for m in g.content.mobs)
        faltan = [x for x in nombres_nuevos if x not in nombres]
        if faltan:
            raise RuntimeError("La relectura no encontró %d subsecuencias." % len(faltan))

    reemplazar_bin(tmp, ruta_bin, mtime0, chequeo, log)
    log("Listo: %d subsecuencias agregadas a %s" % (total, os.path.basename(ruta_bin)))
    return nombres_nuevos


def escribir_temporal(f, ruta_bin):
    carpeta = os.path.dirname(ruta_bin)
    fd, tmp = tempfile.mkstemp(suffix='.subtmp', dir=carpeta)
    os.close(fd)
    try:
        f.write(tmp)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def reemplazar_bin(tmp, ruta_bin, mtime0, chequeo, log):
    """Relee el temporal, verifica, hace backup y reemplaza el bin."""
    try:
        with avb.open(tmp) as g:
            chequeo(g)

        if os.path.getmtime(ruta_bin) != mtime0:
            raise RuntimeError("El bin cambió mientras se procesaba (¿Avid lo guardó?). "
                               "No se tocó nada: cerrá el bin en Avid y probá de nuevo.")

        dest = carpeta_backup(ruta_bin)
        os.makedirs(dest, exist_ok=True)
        base = os.path.splitext(os.path.basename(ruta_bin))[0]
        sello = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(dest, "%s_%s.avb" % (base, sello))
        shutil.copy2(ruta_bin, backup)
        try:
            shutil.copymode(ruta_bin, tmp)
        except OSError:
            pass
        os.replace(tmp, ruta_bin)
        tmp = None
        log("Backup del bin original: %s" % backup)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# subsecuencias -> subclips (como AutoSync)
# ---------------------------------------------------------------------------

class NoConvertible(Exception):
    pass


def resolver_pista(comp, avisos):
    """Reduce una pista de subsecuencia a lo que va en el subclip:
       ('clip', SourceClip)  -> pista directa al clip
       ('efecto', comp)      -> el efecto se mantiene, como hace Avid
       None                  -> pista vacía (solo relleno)
    El FrameFlex (adaptador espacial) no se guarda como efecto: viaja con la
    marca de reformateo del subclip. NoConvertible si no se puede."""
    k = cid(comp)
    if k == SEQU:
        partes = [c for c in comp.components if c.length > 0]
        if any(cid(c) == TNFX for c in partes):
            raise NoConvertible("tiene una transición")
        if not partes or all(cid(c) == FILL for c in partes):
            return None
        if len(partes) > 1:
            if any(cid(c) == FILL for c in partes):
                raise NoConvertible("tiene un hueco (relleno) en parte del plano")
            raise NoConvertible("tiene un corte adentro")
        return resolver_pista(partes[0], avisos)
    if k == FILL:
        return None
    if k == SCLP:
        return ('clip', comp)
    if k == b'SLCT':
        raise NoConvertible("es un multicam/group")

    if es_frameflex(comp):
        avisos.add(FRAMEFLEX)
        for t in comp.tracks:
            c = comp_de(t)
            if c is None or cid(c) in (b'CTRL', b'PRCL'):
                continue
            r = resolver_pista(c, avisos)
            if r is not None:
                return r
        return None

    if hasattr(comp, 'tracks') and not callable(getattr(comp, 'tracks')):
        if primer_clip(comp) is None:
            # efecto sobre una pista vacía: no hay clip que lo lleve
            avisos.add("vacio:" + nombre_efecto(comp))
            return None
        avisos.add("mantiene:" + nombre_efecto(comp))
        return ('efecto', comp)

    raise NoConvertible("tiene un componente desconocido (%s)" % type(comp).__name__)


def primer_clip(comp, prof=0):
    """Primer SourceClip con material dentro de un componente (o None)."""
    if prof > 30 or comp is None:
        return None
    k = cid(comp)
    if k == SCLP:
        return comp
    if k == SEQU:
        for c in comp.components:
            if c.length:
                r = primer_clip(c, prof + 1)
                if r is not None:
                    return r
        return None
    if hasattr(comp, 'tracks') and not callable(getattr(comp, 'tracks')):
        for t in comp.tracks:
            c = comp_de(t)
            if c is not None and cid(c) not in (b'CTRL', b'PRCL'):
                r = primer_clip(c, prof + 1)
                if r is not None:
                    return r
    return None


FRAMEFLEX = "FrameFlex"
BRO = '_BOUNDS_REFORMATTING_OPTION'   # marca de reformateo (FrameFlex) del clip
IBO = '_IMAGE_BOUNDS_OVERRIDE'        # encuadre (Framing) del FrameFlex


def es_frameflex(comp):
    try:
        return (comp.effect_id or '') == 'INT_EFF_SPATIAL_ADAPTER'
    except AttributeError:
        return False


def leer_attr(obj, clave):
    try:
        a = obj.attributes
        if a and clave in a:
            return a[clave]
    except Exception:
        pass
    return None


def encuadre(xml):
    """Texto de <Framing> de un _IMAGE_BOUNDS_OVERRIDE (o None)."""
    if not xml:
        return None
    try:
        i = xml.index('<Framing>') + len('<Framing>')
        return " ".join(xml[i:xml.index('</Framing>', i)].split())
    except ValueError:
        return None


def encuadre_de_origen(f, sc):
    """Framing del FrameFlex guardado en el clip de origen (file mob),
    siguiendo subclip -> master -> file mob dentro del bin. None si no se ve."""
    mob_id, track_id = sc.mob_id, sc.track_id
    for _ in range(10):
        try:
            m = f.content.find_by_mob_id(mob_id)
        except Exception:
            m = None
        if m is None:
            return None
        fr = encuadre(leer_attr(m, IBO))
        if fr and getattr(m, 'mob_type', '') == 'SourceMob':
            return fr
        sig = None
        for t in m.tracks:
            c = comp_de(t)
            if c is None or c.media_kind != 'picture':
                continue
            if t.index != track_id and sig is not None:
                continue
            if cid(c) == SEQU:
                c = next((x for x in c.components if x.length and cid(x) == SCLP), None)
            if c is not None and cid(c) == SCLP:
                sig = c
                if t.index == track_id:
                    break
        if sig is None:
            return fr
        mob_id, track_id = sig.mob_id, sig.track_id
    return None


NOMBRES_EFECTO = {
    'MotionEffect': 'motion effect', 'Repeat': 'freeze frame',
    'PanVolumeEffect': 'clip gain/volumen',
    'StrobeEffect': 'strobe', 'CaptureMask': 'capture mask',
    'TimeWarp': 'timewarp',
}


def nombre_efecto(comp):
    n = type(comp).__name__
    try:
        eid = comp.effect_id
    except AttributeError:
        eid = None
    if eid == 'EFF_TIMEWARP.MOTION_CTL' or (n == 'MotionEffect' and eid):
        return 'speed'
    if n in NOMBRES_EFECTO:
        return NOMBRES_EFECTO[n]
    if eid:
        return str(eid)
    return n


def origen_directo(f, sc):
    """(mob_id, track_id, start) del clip que está en la subsecuencia, tal cual:
    si es un subclip, el subclip nuevo apunta a ESE subclip (match frame lleva
    primero a él y de ahí al master), igual que AutoSync."""
    try:
        m = f.content.find_by_mob_id(sc.mob_id)
    except Exception:
        m = None
    if m is not None and getattr(m, 'mob_type', '') == 'CompositionMob':
        if m.usage_code == 0:
            raise NoConvertible("usa una secuencia anidada")
        if m.usage_code in (4, 5):
            raise NoConvertible("usa un multicam/group")
    return sc.mob_id, sc.track_id, sc.start_time


def limpiar_frameflex(clip):
    """Saca de un SourceClip directo los atributos de FrameFlex (en un subclip
    van en el mob, como hace Avid). Conserva locators y demás."""
    try:
        a = clip.attributes
        if a:
            for k in (BRO, IBO):
                if k in a:
                    del a[k]
            if not len(a):
                clip.attributes = None
    except Exception:
        pass


def crear_subclip(f, sub, nombre, avisos):
    """Subclip (usage 2) con una pista por pista de la subsecuencia.
    - Clip directo: un SourceClip al mismo clip (master o subclip) que tenía.
    - Con efecto (speed, color...): Sequence[relleno, efecto, relleno], el
      efecto tal cual, como el subclip que deja Avid.
    - FrameFlex: marca de reformateo en el mob, encuadre del clip de origen.
    Lanza NoConvertible si no se puede."""
    n = sub.length
    pistas = []
    for t in sub.tracks:
        c = comp_de(t)
        if c is None or c.media_kind not in ('picture', 'sound'):
            continue
        try:
            r = resolver_pista(c, avisos)
        except NoConvertible as e:
            raise NoConvertible("%s %s" % (etiqueta_pista(t), e))
        if r is None:
            continue
        modo, obj = r
        if obj.length != n:
            raise NoConvertible("%s no cubre todo el plano" % etiqueta_pista(t))
        if modo == 'clip':
            try:
                origen_directo(f, obj)
            except NoConvertible as e:
                raise NoConvertible("%s %s" % (etiqueta_pista(t), e))
        pistas.append((t, modo, obj))
    if not pistas:
        raise NoConvertible("no tiene video ni audio")

    nuevo = sub.copy(f)
    nuevo.mob_id = avb.mobid.MobID.new()
    nuevo.name = nombre
    nuevo.usage_code = 2
    ahora = datetime.datetime.now()
    try:
        nuevo.creation_time = ahora
        nuevo.last_modified = ahora
    except Exception:
        pass

    # FrameFlex: marca de reformateo en el mob
    marca = None
    if FRAMEFLEX in avisos:
        for t, modo, obj in pistas:
            if obj.media_kind != 'picture':
                continue
            sc = obj if modo == 'clip' else primer_clip(obj)
            if sc is None:
                continue
            v = leer_attr(sc, BRO)
            if v is None:
                try:
                    v = leer_attr(f.content.find_by_mob_id(sc.mob_id), BRO)
                except Exception:
                    v = None
            if v is not None and marca is None:
                marca = v
            fr_seg = encuadre(leer_attr(sc, IBO))
            if fr_seg:
                fr_ori = encuadre_de_origen(f, sc)
                if fr_ori and fr_ori != fr_seg:
                    avisos.add("FrameFlex distinto")
    try:
        if nuevo.attributes is None:
            nuevo.attributes = f.create.Attributes()
        a = nuevo.attributes
        if IBO in a:
            del a[IBO]           # encuadre de secuencia: un subclip no lo lleva
        if marca is not None:
            a[BRO] = marca
    except Exception:
        pass

    pistas_nuevas = []
    for t, modo, obj in pistas:
        tn = t.copy(f)
        if modo == 'clip':
            clip = obj.copy(f)     # conserva locators del segmento
            clip.length = n
            if FRAMEFLEX in avisos:
                limpiar_frameflex(clip)
            tn.component = clip
        else:
            seq = f.create.Sequence(edit_rate=obj.edit_rate, media_kind=obj.media_kind)
            for comp in (Recortador(f).relleno(obj, 0), obj.copy(f),
                         Recortador(f).relleno(obj, 0)):
                seq.components.append(comp)
            tn.component = seq
        pistas_nuevas.append(tn)
    nuevo.tracks = pistas_nuevas
    nuevo.length = n
    return nuevo


def convertir_en_subclips(ruta_bin, nombres_secuencias, conservar=False,
                          simular=False, log=print):
    """Convierte las subsecuencias elegidas en subclips. Devuelve los nombres."""
    if avb is None:
        raise RuntimeError("Falta la librería pyavb (pip install pyavb).")
    ruta_bin = os.path.abspath(ruta_bin)
    if not os.path.isfile(ruta_bin):
        raise IOError("No existe el bin: %s" % ruta_bin)
    if not nombres_secuencias:
        raise ValueError("Elegí las subsecuencias a convertir.")

    mtime0 = os.path.getmtime(ruta_bin)
    f = avb.open(ruta_bin)
    tmp = None
    try:
        elegidas = [s for s in secuencias_del_bin(f) if s.name in nombres_secuencias]
        if not elegidas:
            raise ValueError("No encontré esas secuencias en el bin.")
        existentes = set(m.name for m in f.content.mobs
                         if getattr(m, 'mob_type', '') == 'CompositionMob')
        avisos = set()
        nuevos, reemplazadas, salteadas = [], [], []
        log("Convertir en subclips: %d secuencias%s" % (
            len(elegidas), " (se conservan las subsecuencias)" if conservar else ""))
        for sub in elegidas:
            nombre = sub.name + SUFIJO_CONSERVAR if conservar else sub.name
            if conservar and nombre in existentes:
                log("  %s   (ya existe %s: se saltea)" % (sub.name, nombre))
                continue
            propios = set()
            try:
                clip = crear_subclip(f, sub, nombre, propios)
            except NoConvertible as e:
                log("  %s   NO SE CONVIERTE: %s" % (sub.name, e))
                salteadas.append(sub.name)
                continue
            avisos |= propios
            mantenidos = sorted(set(x.split(":", 1)[1] for x in propios
                                    if x.startswith("mantiene:")))
            extra = ""
            if FRAMEFLEX in propios:
                extra += "   (FrameFlex: viaja)"
            if mantenidos:
                extra += "   (con efecto: %s)" % ", ".join(mantenidos)
            if "FrameFlex distinto" in propios:
                extra += "   (OJO: FrameFlex retocado en el timeline)"
            vacios = sorted(set(x.split(":", 1)[1] for x in propios
                                if x.startswith("vacio:")))
            if vacios:
                extra += "   (OJO: queda afuera %s, puesto sobre una pista vacía)" % ", ".join(vacios)
            log("  %s  ->  subclip %s  (%d fr)%s" % (sub.name, nombre, sub.length, extra))
            nuevos.append(clip)
            if not conservar:
                reemplazadas.append(sub)
            existentes.add(nombre)

        if "FrameFlex distinto" in avisos:
            log("AVISO: en algún plano el FrameFlex del timeline es distinto al del "
                "clip de origen. El subclip (como en Avid) usa el del clip de origen: "
                "revisalo.")
        if any(x.startswith("vacio:") for x in avisos):
            log("AVISO: un efecto puesto sobre una pista vacía (por ejemplo un color en "
                "V2 encima de V1) no puede ir en el subclip porque no tiene clip debajo.")
        if salteadas:
            log("%d no se convirtieron: un subclip no puede tener cortes, "
                "transiciones, huecos ni multicam adentro." % len(salteadas))

        if simular:
            log("Simulación: no se escribió nada.")
            return []
        if not nuevos:
            log("No hay subclips para crear.")
            return []

        if reemplazadas:
            quitar = set(id(m) for m in reemplazadas)
            items = f.content.items
            for i in reversed(range(len(items))):
                if id(items[i].mob) in quitar:
                    items.pop(i)
            try:
                f.content._mob_dict = {}
            except Exception:
                pass
        for m in nuevos:
            f.content.add_mob(m)
        nombres_nuevos = [m.name for m in nuevos]
        tmp = escribir_temporal(f, ruta_bin)
    finally:
        f.close()

    def chequeo(g):
        subclips = set(m.name for m in g.content.mobs
                       if getattr(m, 'mob_type', '') == 'CompositionMob'
                       and m.usage_code == 2)
        faltan = [x for x in nombres_nuevos if x not in subclips]
        if faltan:
            raise RuntimeError("La relectura no encontró %d subclips." % len(faltan))

    reemplazar_bin(tmp, ruta_bin, mtime0, chequeo, log)
    log("Listo: %d subclips en %s%s" % (
        len(nombres_nuevos), os.path.basename(ruta_bin),
        "" if conservar else " (reemplazaron a sus subsecuencias)"))
    return nombres_nuevos


def leer_bin(ruta_bin):
    """[(nombre, largo, [etiquetas de pistas], tc inicio)] de las secuencias."""
    res = []
    with avb.open(ruta_bin) as f:
        for mob in secuencias_del_bin(f):
            inicio, fps, drop = tc_de_secuencia(mob)
            res.append((mob.name, mob.length, [e for e, _ in pistas_av(mob)],
                        frames_a_tc(inicio, fps, drop)))
    return res


def opciones_pistas(etiquetas):
    """Lo que se ofrece en el menú 'Cortes de'."""
    vids = [e for e in etiquetas if e.startswith('V')]
    auds = [e for e in etiquetas if e.startswith('A')]
    ops = list(vids)
    if len(vids) > 1:
        ops.append("Todas las de video")
    ops += auds
    if auds:
        ops.append("Todas")
    return ops


def resolver_opcion(opcion, etiquetas):
    if opcion == "Todas las de video":
        return [e for e in etiquetas if e.startswith('V')]
    if opcion == "Todas":
        return list(etiquetas)
    return [opcion]


# ---------------------------------------------------------------------------
# ventana
# ---------------------------------------------------------------------------

def app(ruta_inicial=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        raiz = TkinterDnD.Tk()
        dnd = True
    except Exception:
        raiz = tk.Tk()
        dnd = False

    raiz.title("Subsecuencias Avid %s" % VERSION)
    raiz.geometry("780x720")
    raiz.minsize(620, 480)

    estado = {'bin': None, 'secuencias': []}
    v_bin = tk.StringVar()
    v_pista = tk.StringVar()
    v_nombre = tk.StringVar(value=NOMBRE_DEFECTO)
    v_add = tk.BooleanVar(value=True)
    v_huecos = tk.BooleanVar(value=False)
    v_conservar = tk.BooleanVar(value=False)

    marco = ttk.Frame(raiz, padding=10)
    marco.pack(fill='both', expand=True)

    zona = tk.Label(marco, text=("Soltá acá el bin (.avb)" if dnd else
                                 "Elegí el bin (.avb)") +
                    "\n\nAvid puede quedar abierto: cerrá solo ese bin.",
                    relief='groove', bd=2, height=4, bg='#f2f2f2', fg='#222222')
    zona.pack(fill='x')

    fila = ttk.Frame(marco)
    fila.pack(fill='x', pady=(6, 6))
    ttk.Entry(fila, textvariable=v_bin, state='readonly').pack(side='left', fill='x', expand=True)
    ttk.Button(fila, text="Elegir…", command=lambda: elegir()).pack(side='left', padx=(6, 0))

    ttk.Label(marco, text="Secuencias del bin (Ctrl/Cmd o Mayús para elegir varias):").pack(anchor='w')
    lista = tk.Listbox(marco, height=7, exportselection=False, selectmode='extended')
    lista.pack(fill='x')

    # --- 1: subsecuencias ---------------------------------------------------
    opc = ttk.LabelFrame(marco, text=" 1. Subsecuencias por corte ", padding=8)
    opc.pack(fill='x', pady=(8, 0))
    ttk.Label(opc, text="Cortes de:").grid(row=0, column=0, sticky='w')
    combo = ttk.Combobox(opc, textvariable=v_pista, state='readonly', width=22)
    combo.grid(row=0, column=1, sticky='w', padx=(4, 16))
    ttk.Label(opc, text="Nombre:").grid(row=0, column=2, sticky='w')
    ttk.Entry(opc, textvariable=v_nombre, width=30).grid(row=0, column=3, sticky='we', padx=(4, 0))
    opc.columnconfigure(3, weight=1)
    ttk.Label(opc, text="{secuencia} {n} {clip} {tc}", foreground='#777').grid(
        row=1, column=3, sticky='w', padx=(4, 0))
    ttk.Checkbutton(opc, text="Ignorar add edits (mismo clip sin salto)",
                    variable=v_add).grid(row=2, column=0, columnspan=2, sticky='w', pady=(4, 0))
    ttk.Checkbutton(opc, text="Crear también los huecos (relleno)",
                    variable=v_huecos).grid(row=2, column=2, columnspan=2, sticky='w', pady=(4, 0))
    bot = ttk.Frame(opc)
    bot.grid(row=3, column=0, columnspan=4, sticky='w', pady=(8, 0))
    b_sim = ttk.Button(bot, text="Previsualizar", command=lambda: correr(True))
    b_sim.pack(side='left')
    b_ok = ttk.Button(bot, text="Crear subsecuencias", command=lambda: correr(False))
    b_ok.pack(side='left', padx=6)

    # --- 2: subclips ----------------------------------------------------------
    opc2 = ttk.LabelFrame(marco, text=" 2. Subsecuencias → subclips (como AutoSync) ", padding=8)
    opc2.pack(fill='x', pady=(8, 8))
    ttk.Label(opc2, text="Convierte las secuencias elegidas en la lista. "
              "Cada pista apunta al clip que tenía (subclip o master).",
              foreground='#555').grid(row=0, column=0, columnspan=3, sticky='w')
    ttk.Checkbutton(opc2, text="Conservar las subsecuencias (el subclip se llama " +
                    "«nombre%s»)" % SUFIJO_CONSERVAR,
                    variable=v_conservar).grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 0))
    bot2 = ttk.Frame(opc2)
    bot2.grid(row=2, column=0, columnspan=3, sticky='w', pady=(8, 0))
    b_sim2 = ttk.Button(bot2, text="Previsualizar", command=lambda: convertir(True))
    b_sim2.pack(side='left')
    b_ok2 = ttk.Button(bot2, text="Convertir en subclips", command=lambda: convertir(False))
    b_ok2.pack(side='left', padx=6)
    botones = (b_sim, b_ok, b_sim2, b_ok2)

    fuente = ('Menlo', 11) if sys.platform == 'darwin' else ('Consolas', 9)
    txt = tk.Text(marco, height=14, wrap='none', font=fuente)
    txt.pack(fill='both', expand=True)

    def log(m):
        txt.insert('end', m + "\n")
        txt.see('end')
        raiz.update_idletasks()

    def seleccionadas():
        return [estado['secuencias'][i] for i in lista.curselection()]

    def actualizar_pistas(*_):
        sel = seleccionadas() or estado['secuencias'][:1]
        if not sel:
            combo['values'] = []
            return
        comunes = None
        for s in sel:
            comunes = s[2] if comunes is None else [e for e in comunes if e in s[2]]
        ops = opciones_pistas(comunes or [])
        combo['values'] = ops
        if v_pista.get() not in ops:
            v_pista.set(ops[0] if ops else "")

    lista.bind('<<ListboxSelect>>', actualizar_pistas)

    def cargar(ruta):
        ruta = ruta.strip().strip('{}').strip('"')
        if not ruta.lower().endswith('.avb'):
            messagebox.showwarning("Subsecuencias", "Eso no es un bin (.avb).")
            return
        try:
            secs = leer_bin(ruta)
        except Exception as e:
            messagebox.showerror("Subsecuencias", "No pude leer el bin:\n%s" % e)
            return
        estado['bin'] = ruta
        estado['secuencias'] = secs
        v_bin.set(ruta)
        lista.delete(0, 'end')
        for nombre, largo, pist, tc in secs:
            lista.insert('end', "%s   [%s, %d fr, %s]" % (nombre, tc, largo, " ".join(pist)))
        if secs:
            lista.selection_set(0)
        actualizar_pistas()
        txt.delete('1.0', 'end')
        log("Bin: %s  (%d secuencias)" % (os.path.basename(ruta), len(secs)))

    def elegir():
        r = filedialog.askopenfilename(filetypes=[("Bins de Avid", "*.avb")])
        if r:
            cargar(r)

    def soltar(ev):
        rutas = raiz.tk.splitlist(ev.data)
        if rutas:
            cargar(rutas[0])

    if dnd:
        for w in (zona, lista, txt):
            w.drop_target_register(DND_FILES)
            w.dnd_bind('<<Drop>>', soltar)
    zona.bind('<Button-1>', lambda e: elegir())

    def correr(simular):
        if not estado['bin']:
            messagebox.showinfo("Subsecuencias", "Primero soltá o elegí un bin.")
            return
        sel = seleccionadas()
        if not sel:
            messagebox.showinfo("Subsecuencias", "Elegí una secuencia.")
            return
        comunes = sel[0][2]
        etiquetas = resolver_opcion(v_pista.get(), comunes)
        txt.delete('1.0', 'end')
        for b in botones:
            b.state(['disabled'])
        try:
            creadas = procesar(estado['bin'], [s[0] for s in sel], etiquetas,
                               v_nombre.get() or NOMBRE_DEFECTO, v_add.get(),
                               v_huecos.get(), simular, log)
            if not simular:
                if creadas:
                    log("\nLas subsecuencias nuevas quedaron elegidas en la lista: "
                        "con \u00abConvertir en subclips\u00bb pasan a subclips.")
                log("Volvé a abrir el bin en Avid para verlas.")
                cargar_silencioso(creadas or None)
        except Exception as e:
            log("ERROR: %s" % e)
            log(traceback.format_exc())
        finally:
            for b in botones:
                b.state(['!disabled'])

    def convertir(simular):
        if not estado['bin']:
            messagebox.showinfo("Subsecuencias", "Primero soltá o elegí un bin.")
            return
        sel = seleccionadas()
        if not sel:
            messagebox.showinfo("Subsecuencias", "Elegí en la lista las subsecuencias a convertir.")
            return
        if not simular and not v_conservar.get():
            if not messagebox.askyesno(
                    "Subsecuencias",
                    "Se van a convertir %d secuencias en subclips y las secuencias "
                    "se quitan del bin (queda backup).\n\n¿Seguimos?" % len(sel)):
                return
        txt.delete('1.0', 'end')
        for b in botones:
            b.state(['disabled'])
        try:
            convertir_en_subclips(estado['bin'], [s[0] for s in sel],
                                  v_conservar.get(), simular, log)
            if not simular:
                log("Volvé a abrir el bin en Avid para ver los subclips.")
                cargar_silencioso()
        except Exception as e:
            log("ERROR: %s" % e)
            log(traceback.format_exc())
        finally:
            for b in botones:
                b.state(['!disabled'])

    def cargar_silencioso(elegir_nombres=None):
        try:
            secs = leer_bin(estado['bin'])
            sel = elegir_nombres or [s[0] for s in seleccionadas()]
            estado['secuencias'] = secs
            lista.delete(0, 'end')
            for i, (nombre, largo, pist, tc) in enumerate(secs):
                lista.insert('end', "%s   [%s, %d fr, %s]" % (nombre, tc, largo, " ".join(pist)))
                if nombre in sel:
                    lista.selection_set(i)
                    lista.see(i)
            actualizar_pistas()
        except Exception:
            pass

    if ruta_inicial:
        raiz.after(100, lambda: cargar(ruta_inicial))
    raiz.mainloop()


# ---------------------------------------------------------------------------

def main(argv):
    if '--cli' in argv:
        import argparse
        p = argparse.ArgumentParser(description="Subsecuencias por corte (Avid)")
        p.add_argument('--cli', action='store_true')
        p.add_argument('bin')
        p.add_argument('--secuencia', action='append')
        p.add_argument('--pistas', default='V1', help="V1 | V1,V2 | video | todas")
        p.add_argument('--nombre', default=NOMBRE_DEFECTO)
        p.add_argument('--simular', action='store_true')
        p.add_argument('--con-huecos', action='store_true')
        p.add_argument('--sin-unir-add-edits', action='store_true')
        p.add_argument('--subclips', action='store_true',
                       help="convertir las --secuencia indicadas en subclips")
        p.add_argument('--conservar', action='store_true')
        a = p.parse_args(argv)
        secs = leer_bin(a.bin)
        if not secs:
            print("El bin no tiene secuencias.")
            return 1
        if a.subclips:
            convertir_en_subclips(a.bin, a.secuencia or [], a.conservar, a.simular)
            return 0
        elegidas = a.secuencia or [secs[0][0]]
        pist = next(s[2] for s in secs if s[0] == elegidas[0])
        if a.pistas.lower() == 'video':
            etiquetas = resolver_opcion("Todas las de video", pist)
        elif a.pistas.lower() == 'todas':
            etiquetas = resolver_opcion("Todas", pist)
        else:
            etiquetas = [x.strip().upper() for x in a.pistas.split(',')]
        procesar(a.bin, elegidas, etiquetas, a.nombre, not a.sin_unir_add_edits,
                 a.con_huecos, a.simular)
        return 0
    ruta = next((x for x in argv if x.lower().endswith('.avb')), None)
    app(ruta)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
