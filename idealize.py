# Python Script, API Version = V252
# -*- coding: utf-8 -*-
# =============================================================================
#  idealize.py  -  SpaceClaim (IronPython 2.7) : идеализация корпусных конструкций
#
#  Запуск: SpaceClaim -> вкладка Script -> Open script -> Run
#  Совместимость: IronPython 2.7 (БЕЗ f-строк, type hints, py3-синтаксиса).
#
#  Цель: из твердотельной модели получить расчётную модель для ANSYS
#        режим "shell+beam"  (основной)  или  "solid+shell" (локализация).
#        Эти три типа общей топологией НЕ объединяются (solid / shell / beam
#        живут в отдельных группах и сшиваются только контактами в Mechanical).
#
#  Этапы:
#     1. SHEETS     листы         -> поверхность + толщина + сторона выкладки
#     2. STIFFENERS полособульбы  -> балки (через экструзию сечения)
#                                    [альтернатива: полоса с эквивалентным W]
#     3. SMALL      кницы и т.п.  -> поверхности
#     4. HEALING    продление поверхностей / закрытие зазоров
#
#  СТАТУС: шаблон. Функции с пометкой [TODO] требуют доработки/проверки
#  на реальной геометрии; API-вызовы SpaceClaim, в которых нет уверенности,
#  собраны в блоке "SC API ADAPTER" - править нужно только там.
# =============================================================================
import clr
import math
import os

# API SpaceClaim (GetRootPart, Selection, Body, DesignBody ...) в скрипте доступно
# БЕЗ import: версию задаёт первая строка файла "# Python Script, API Version = ...".
# Если не стартует - запишите любой скрипт через Record и скопируйте оттуда эту строку.


# =============================================================================
# 0. КОНФИГУРАЦИЯ
# =============================================================================
def _reraise_abort(ex):
    """Остановку скрипта (кнопка Stop / ScriptAbortedException) нельзя глотать в
    try/except - иначе скрипт невозможно прервать и он выглядит зациклившимся."""
    txt = "%s %s" % (type(ex).__name__, ex)
    if "ScriptAborted" in txt or "interrupted" in txt or isinstance(ex, KeyboardInterrupt):
        raise ex


class Cfg(object):
    # --- режим модели ---
    MODEL_MODE = "shell+beam"        # "shell+beam" | "solid+shell"

    # --- классификация листа ---
    SHEET_RATIO = 10.0               # мин. отношение (средний габарит / наименьший)
    SHEET_MAX_T = 60.0               # мм, верхняя граница толщины листа (защита)
    # Единицы модели SpaceClaim: метры (внутри API). Ниже - пересчёт в мм.
    UNIT_TO_MM = 1000.0

    # --- сторона выкладки (offset в Mechanical) ---
    # Если команда/нормаль не определена -> используем BOT / MID / TOP
    DEFAULT_OFFSET = "MID"           # "BOT" | "MID" | "TOP"
    # Какую поверхность листа копировать:
    #   "AUTO"  - сторону, обращённую к ближайшей связи (контакт) [TODO]
    #   "MID"   - срединная
    #   "BOT"/"TOP" - по знаку нормали относительно REF_AXIS
    COPY_SIDE = "MID"
    REF_AXIS = (0.0, 0.0, 1.0)       # для BOT/TOP: TOP = грань с нормалью вдоль REF_AXIS

    # --- классификация полособульба ---
    STIFF_ASPECT_MIN = 8.0           # длина / макс. размер сечения
    STIFF_MAX_SECTION = 0.45         # м, макс. габарит сечения

    # --- балки ---
    # На старте полособульбы - ТОЛЬКО балки по сечению (эквивалентная полоса отложена)
    BEAM_MODE = "BEAM_FROM_SECTION"
    BEAM_OFFSET_TO_PLATE = True      # [TODO] сдвиг оси балки на поверхность обшивки

    # --- малые связи ---
    SMALL_MAX_AREA = 0.5             # м^2, всё что мельче и не лист/профиль -> кница

    # --- зазоры ---
    GAP_TOL = 0.010                  # м, закрывать зазоры <= 10 мм
    EXTEND_MAX = 0.050               # м, макс. величина продления

    # --- вывод ---
    OUT_DIR = None                   # None -> Documents, иначе temp
    REPORT_NAME = "idealize_report.csv"
    DRY_RUN = False                  # True: только классификация и отчёт, без правок
    # Что делать с исходным солидом ПОСЛЕ того, как поверхность создана и проверена
    # (скрытие не исключает тело из расчёта):
    #   "SUPPRESS" - исключить из физики (Suppress for Physics), обратимо
    #   "DELETE" - удалить (Ctrl+Z откатывает), "HIDE" - скрыть, "NONE" - оставить
    ORIGINAL_ACTION = "SUPPRESS"
    # Множители верхней границы толщины (к толщине листа); пробуются по очереди,
    # пока Midsurface.Convert не вернёт Success
    MIDSURFACE_FACTORS = (1.1, 2.0, 5.0)
    # Если Midsurface.Convert не дала поверхность - построить через копирование грани
    FALLBACK_TO_FACE = True
    PROBE_API = True                 # в начале напечатать, какие вызовы API доступны

    # Для COPY_SIDE="MID": использовать штатную Midsurface.Convert (на копии тела).
    # SpaceClaim сам назначает толщину; работает и на гнутых листах.
    USE_MIDSURFACE = False           # ВЫКЛ: команда давала лавину поверхностей

    # --- защита от лавины тел ---
    LIMIT = 5                        # обработать не более N листов за запуск (None = все)
    ONLY_SELECTED = False            # True: брать только выделенные тела
    SKIP_PREFIXES = ("SHEET_", "BEAM_", "SMALL_", "ORIG_")   # уже обработанное

    # --- гнутые (неплоские) листы ---
    # Копируется наибольшая грань листа (без сдвига), толщина откладывается от неё.
    # Нормаль копии считаем совпадающей с внешней нормалью исходной грани -> материал
    # с отрицательной стороны -> "TOP".  [TODO] подтвердить в Mechanical; иначе "BOT".
    CURVED_OFFSET = "TOP"
    CURVED_SIDE = "LARGER"           # какую сторону листа копировать: "LARGER" | "SMALLER" (по площади)

    # --- какие этапы выполнять (сейчас готов только 1) ---
    STAGES = {"sheets": True, "stiffeners": False, "small": False, "healing": False}


# =============================================================================
# 1. SC API ADAPTER  (единственное место с вызовами SpaceClaim API)
#    Все функции ниже [TODO: проверить на вашей версии]
# =============================================================================
class SC(object):
    @staticmethod
    def root():
        return GetRootPart()  # noqa

    @staticmethod
    def design_bodies(part, _seen=None):
        """Все тела (DesignBody) узла, рекурсивно по компонентам (каждый Template 1 раз)."""
        seen = _seen if _seen is not None else []
        if any(part is x for x in seen):
            return []
        seen.append(part)
        out = list(part.Bodies)
        for comp in part.Components:
            out.extend(SC.design_bodies(comp.Template, seen))
        return out

    @staticmethod
    def selected_bodies():
        """Выделенные DesignBody или None, если выделение недоступно."""
        try:
            return list(Selection.GetActive().GetItems[IDesignBody]())  # noqa
        except Exception:
            return None

    @staticmethod
    def part_of(body):
        """Part, в котором создавать результат (родитель исходного тела, иначе root)."""
        p = getattr(body, "Parent", None)
        return p if isinstance(p, Part) else SC.root()  # noqa

    @staticmethod
    def is_solid(body):
        return body.Shape.IsClosed if hasattr(body.Shape, "IsClosed") else True

    @staticmethod
    def bbox_size(body):
        """Габариты по осям мирового bbox (м) -> (dx, dy, dz)."""
        box = body.Shape.GetBoundingBox(Matrix.Identity)  # noqa
        s = box.Size
        return (s.X, s.Y, s.Z)

    @staticmethod
    def face_normal(face_shape):
        """Внешняя нормаль плоской грани (Modeler.Face) с учётом IsReversed -> tuple."""
        n = face_shape.Geometry.Frame.DirZ
        if face_shape.IsReversed:
            n = -n
        return (n.X, n.Y, n.Z)

    _pf_diag_done = [False]

    @staticmethod
    def planar_faces(body):
        """[(face, area, unit_normal(tuple), origin Point)] только плоские грани."""
        res = []
        for f in body.Faces:
            try:
                surf = f.Shape.Geometry
                if isinstance(surf, Plane):  # noqa
                    res.append((f, f.Shape.Area, SC.face_normal(f.Shape), surf.Frame.Origin))
            except Exception as ex:
                _reraise_abort(ex)
                if not SC._pf_diag_done[0]:
                    SC._pf_diag_done[0] = True
                    print("    planar_faces: грань пропущена (%s)" % ex)
                continue
        return res

    @staticmethod
    def extent_along(body, direction):
        """Протяжённость тела вдоль единичного вектора (м): max-min проекций вершин."""
        proj = []
        for v in body.Shape.Vertices:  # [TODO] при отсутствии Vertices -> по рёбрам
            p = v.Position
            proj.append(p.X * direction[0] + p.Y * direction[1] + p.Z * direction[2])
        return (max(proj) - min(proj)) if proj else 0.0

    @staticmethod
    def volume(body):
        try:
            return abs(body.Shape.Volume)
        except Exception:
            return None

    @staticmethod
    def faces_by_area(body):
        """[(DesignFace, area, is_plane)] по убыванию площади."""
        res = []
        for f in body.Faces:
            try:
                res.append((f, f.Shape.Area, isinstance(f.Shape.Geometry, Plane)))  # noqa
            except Exception as ex:
                _reraise_abort(ex)
        res.sort(key=lambda x: -x[1])
        return res

    @staticmethod
    def face_perimeter(face):
        try:
            return sum(e.Shape.Length for e in face.Edges)
        except Exception as ex:
            _reraise_abort(ex)
            return None

    @staticmethod
    def sheet_sides(fa, vol):
        """Разобрать листовое тело на две СТОРОНЫ (каждая - набор граней, в т.ч.
        плоские + гнутые участки) и торцы. Возвращает (t, большая_сторона, меньшая)
        как списки (face, area, is_plane) или None.
          1) оценка t0 = 2V / A_полн;
          2) торец = грань с "шириной" 2A/P порядка t0, остальное - стороны;
          3) стороны = 2 связные группы граней (соседство через общие рёбра):
             между собой две стороны соединены только через торцы;
          4) t = V / A_ср_сторон."""
        if not vol or len(fa) < 2:
            return None
        t0 = 2.0 * vol / sum(x[1] for x in fa)
        amax = fa[0][1]
        side = []
        for x in fa:
            per = SC.face_perimeter(x[0])
            w = (2.0 * x[1] / per) if per else x[1] / math.sqrt(amax)
            if w > 3.0 * t0:
                side.append(x)
        n = len(side)
        if n < 2:
            return None
        comp = list(range(n))

        def find(k):
            while comp[k] != k:
                k = comp[k]
            return k

        for k in range(n):
            try:
                edges = list(side[k][0].Edges)
            except Exception:
                edges = []
            for e in edges:
                try:
                    nbs = list(e.Faces)
                except Exception:
                    nbs = []
                for g in nbs:
                    for m in range(n):
                        if m != k and side[m][0] == g:
                            comp[find(m)] = find(k)
        groups = {}
        for k in range(n):
            groups.setdefault(find(k), []).append(side[k])
        if len(groups) != 2:
            print("    sheet_sides: сторон=%d (ожидалось 2), граней-сторон=%d" % (len(groups), n))
            return None
        g = sorted(groups.values(), key=lambda gr: -sum(x[1] for x in gr))
        a0, a1 = sum(x[1] for x in g[0]), sum(x[1] for x in g[1])
        return vol / ((a0 + a1) / 2.0), g[0], g[1]

    @staticmethod
    def copy_face_generic(part, body, faces, name):
        """Скопировать набор ЛЮБЫХ (в т.ч. неплоских) граней в одно поверхностное тело.
        Перебор вариантов API; при провале печатает подходящие члены Body для доработки."""
        from System.Collections.Generic import List  # noqa
        log = []

        fss = [f.Shape for f in faces]

        def a_pylist():
            return body.Shape.CopyFaces(fss)

        def a_typed_runtime():
            fl = List[type(fss[0])]()
            for x in fss:
                fl.Add(x)
            return body.Shape.CopyFaces(fl)

        def a_typed_base():
            ft = type(fss[0])
            while ft.__name__ != "Face" and ft.__bases__:
                ft = ft.__bases__[0]
            fl = List[ft]()
            for x in fss:
                fl.Add(x)
            return body.Shape.CopyFaces(fl)

        def a_all_then_delete():
            # копия всего тела, затем удалить все грани кроме нужных (по индексам)
            allf = list(body.Shape.Faces)
            keep = set(allf.index(x) for x in fss)
            nb = body.Shape.Copy()
            nb.DeleteFaces([f for k, f in enumerate(list(nb.Faces)) if k not in keep], 0)
            return nb

        for nm, fn in (("Body.CopyFaces([face])", a_pylist),
                       ("Body.CopyFaces(List[type(face)])", a_typed_runtime),
                       ("Body.CopyFaces(List[Face base])", a_typed_base),
                       ("Body.Copy + DeleteFaces", a_all_then_delete)):
            try:
                nb = fn()
                db = DesignBody.Create(part, name, nb)  # noqa
                print("    грань скопирована способом: %s" % nm)
                return db
            except Exception as ex:
                _reraise_abort(ex)
                log.append("%s -> %s" % (nm, ex))
        print("    НЕПЛОСКАЯ ГРАНЬ НЕ СКОПИРОВАНА. Диагностика API:")
        for l in log:
            print("      %s" % l)
        keys = ("copy", "face", "offset", "sheet", "surface", "extract", "create")
        for tn, obj in (("Body", body.Shape), ("Face", fss[0])):
            try:
                print("      %s: %s" % (tn, [a for a in dir(obj)
                                           if any(k in a.lower() for k in keys)]))
            except Exception:
                pass
        raise Exception("неплоская грань: способ копирования не подобран (см. лог)")

    @staticmethod
    def _edge_curve(edge):
        """ITrimmedCurve ребра (DesignEdge). Перебор вариантов V25; при полном провале
        печатает доступные атрибуты Edge для доработки."""
        shp = edge.Shape
        try:
            return shp.TrimmedCurve
        except Exception:
            pass
        for attr in ("Bounds", "Interval", "ParameterRange", "Range"):
            try:
                return CurveSegment.Create(shp.Geometry, getattr(shp, attr))  # noqa
            except Exception:
                continue
        print("    Edge attrs: %s" % [a for a in dir(shp) if not a.startswith("_")])
        raise Exception("не удалось получить кривую ребра")

    @staticmethod
    def copy_face_to_surface(part, face, name, shift=None):
        """Скопировать грань (DesignFace) в новое поверхностное тело, при необходимости
        сдвинув на вектор shift=(dx,dy,dz) [м]. Возвращает (DesignBody, normal tuple).

        Плоская грань: Body.CreatePlanarBody(plane, все кромки грани) - внешний контур
        и отверстия идут одним списком кривых. Это самый предсказуемый путь.
        Неплоская грань: fallback copy/paste (см. _copy_paste_face).  [TODO]
        """
        surf = face.Shape.Geometry
        if not isinstance(surf, Plane):  # noqa
            if shift:
                raise NotImplementedError("сдвиг неплоской грани (срединная) - [TODO]")
            return SC._copy_paste_face(part, face, name)

        from System.Collections.Generic import List  # noqa
        curves = List[ITrimmedCurve]()  # noqa
        for e in face.Edges:
            curves.Add(SC._edge_curve(e))
        body = Body.CreatePlanarBody(surf, curves)  # noqa
        if shift:
            body.Transform(Matrix.CreateTranslation(  # noqa
                Vector.Create(shift[0], shift[1], shift[2])))  # noqa
        db = DesignBody.Create(part, name, body)  # noqa
        n = SC.face_normal(list(db.Faces)[0].Shape)
        return db, n

    @staticmethod
    def _copy_paste_face(part, face, name):
        """Fallback для неплоских граней через Copy/Paste.  [TODO: подобрать вызовы V25;
        проще всего записать скрипт (Record) копирования грани и вставить сюда]"""
        raise NotImplementedError("неплоская грань: copy/paste не реализован")

    @staticmethod
    def is_deleted(obj):
        try:
            return bool(obj.IsDeleted)
        except Exception:
            try:
                obj.Name
                return False
            except Exception:
                return True

    @staticmethod
    def set_thickness(db, t, offset="MID"):
        """Присвоить толщину (м) и сторону выкладки поверхностному телу.
        MidSurfaceAspect.Create(db, t) в V25 ЗАМЕНЯЕТ тело: старый DesignBody удаляется,
        появляется новый (с аспектом). Поэтому возвращаем (ok, актуальное_тело) -
        вызывающий обязан дальше работать с возвращённым телом."""
        part = SC.part_of(db)
        before = list(part.Bodies)
        name = getattr(db, "Name", None)
        try:
            asp = MidSurfaceAspect.Create(db, t)  # noqa
        except Exception as ex:
            _reraise_abort(ex)
            print("    толщина НЕ задана: MidSurfaceAspect.Create -> %s" % ex)
            return False, db

        # найти актуальное тело: владелец аспекта -> само тело (если живо) -> новое в part
        body = None
        try:
            if asp is not None and asp.Owner is not None:
                body = asp.Owner
        except Exception:
            pass
        if body is None and not SC.is_deleted(db):
            body = db
        if body is None:
            new = [b for b in part.Bodies if not any(b == x for x in before)]
            if len(new) == 1:
                body = new[0]
        if body is None:
            print("    толщина задана, но новое тело не найдено (переименуйте вручную)")
            return False, db
        if name:
            SC.rename(body, name)

        # сторона выкладки
        try:
            a2 = body.GetMidSurfaceAspect()
            ot = MidSurfaceOffsetType  # noqa
            a2.OffsetType = {"TOP": ot.Top, "BOT": ot.Bottom, "MID": ot.Middle}[offset]
            print("    толщина: t=%s м, offset=%s" % (
                SC._safe(lambda: a2.GetThickness()), SC._safe(lambda: a2.OffsetType)))
        except Exception as ex:
            _reraise_abort(ex)
            print("    толщина задана, offset НЕ задан: %s" % ex)
        return True, body

    @staticmethod
    def hide(body):
        """Скрыть тело (вызов взят из Record V25). Возвращает True/False."""
        try:
            ViewHelper.SetObjectVisibility(  # noqa
                BodySelection.Create(body), VisibilityType.Hide, False, False)  # noqa
            return True
        except Exception:
            return False

    @staticmethod
    def is_sheet(body):
        """True, если тело - поверхность (не твердое).  Перебор признаков V25:
        объём ~ 0 -> не замкнуто -> размер bbox по одной оси ~ 0."""
        try:
            return abs(body.Shape.Volume) < 1e-12
        except Exception:
            pass
        try:
            return not body.Shape.IsClosed
        except Exception:
            pass
        try:
            return min(SC.bbox_size(body)) < 1e-9
        except Exception:
            return False

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as ex:
            return "ERR(%s)" % ex

    @staticmethod
    def _safe_len(fn):
        try:
            return len(list(fn()))
        except Exception as ex:
            return "ERR(%s)" % ex

    @staticmethod
    def delete(obj):
        try:
            obj.Delete()
            return True
        except Exception:
            return False

    @staticmethod
    def suppress(body):
        """Исключить тело из физики (вызов из Record V25). Сначала по телу, затем по граням."""
        try:
            ViewHelper.SetSuppressForPhysics(BodySelection.Create(body), True)  # noqa
            return True
        except Exception:
            pass
        try:
            ViewHelper.SetSuppressForPhysics(  # noqa
                FaceSelection.Create(list(body.Faces)), True)  # noqa
            return True
        except Exception:
            return False

    @staticmethod
    def dispose_original(body):
        """Действие над исходным солидом по Cfg.ORIGINAL_ACTION. Возвращает True/False."""
        a = Cfg.ORIGINAL_ACTION
        if a == "SUPPRESS":
            return SC.suppress(body)
        if a == "DELETE":
            return SC.delete(body)
        if a == "HIDE":
            return SC.hide(body)
        return True

    @staticmethod
    def midsurface_convert(part, body, name, max_t):
        """Срединная поверхность штатной Midsurface.Convert (Record V25) на КОПИИ тела.
        Результат принимается только если это реально поверхность (is_sheet); иначе
        копия удаляется, возвращается None (исходник не тронут).
        max_t - верхняя граница толщины, м."""
        print("    создаю копию..."); _flush()
        copy = DesignBody.Create(part, name, body.Shape.Copy())  # noqa
        before = list(part.Bodies)
        res = None
        for f in Cfg.MIDSURFACE_FACTORS:
            mt = max_t * f / Cfg.MIDSURFACE_FACTORS[0]
            print("    Midsurface.Convert (max_t=%.4f м)..." % mt); _flush()
            res = Midsurface.Convert(BodySelection.Create(copy), mt)  # noqa
            ok = None
            try:
                ok = res.Success
            except Exception:
                pass
            print("    Success=%s, CreatedMidsurfaces=%s" % (
                ok, SC._safe_len(lambda: res.CreatedMidsurfaces)))
            if ok is not False:
                break
        created, modified = [], []
        try:
            created = list(res.GetCreated[IDesignBody]())      # noqa
        except Exception:
            pass
        try:
            modified = list(res.GetModified[IDesignBody]())    # noqa
        except Exception:
            pass
        try:
            mids = list(res.CreatedMidsurfaces)   # возможно, это и есть тела-результаты
            print("    тип CreatedMidsurfaces[0]: %s" % (type(mids[0]).__name__ if mids else "-"))
            created = created + [m for m in mids if hasattr(m, "Faces")]
        except Exception:
            pass
        diff = [b for b in part.Bodies if not any(b is x for x in before)]
        print("    GetCreated=%d GetModified=%d diff=%d тел до=%d после=%d" % (
            len(created), len(modified), len(diff), len(before), len(list(part.Bodies))))
        try:
            print("    copy: Volume=%s граней=%s bbox=%s" % (
                SC._safe(lambda: copy.Shape.Volume),
                SC._safe_len(lambda: copy.Faces), SC._safe(lambda: SC.bbox_size(copy))))
        except Exception:
            pass
        cands = []
        for b in created + modified + diff + [copy]:
            if not any(b is x for x in cands):
                cands.append(b)
        sheets = [b for b in cands if SC.is_sheet(b)]
        print("    кандидатов=%d, поверхностей=%d" % (len(cands), len(sheets)))
        if len(sheets) == 1:
            if copy is not sheets[0]:
                SC.delete(copy)          # копия-солид больше не нужна
            return sheets[0]
        if len(sheets) > 1:
            raise Abort("Midsurface.Convert создала %d поверхностей вместо 1 (тело '%s'). "
                        "Остановлено - проверьте модель (Ctrl+Z)." % (len(sheets), name))
        # не получилось: убрать мусор, исходник не трогаем
        for b in diff + [copy]:
            SC.delete(b)
        return None

    @staticmethod
    def probe():
        """Напечатать доступность вызовов API (диагностика под вашу сборку)."""
        checks = [
            ("Body.CreatePlanarBody", lambda: Body.CreatePlanarBody),         # noqa
            ("DesignBody.Create", lambda: DesignBody.Create),                 # noqa
            ("CurveSegment.Create", lambda: CurveSegment.Create),             # noqa
            ("Matrix.CreateTranslation", lambda: Matrix.CreateTranslation),   # noqa
            ("Vector.Create", lambda: Vector.Create),                         # noqa
            ("Body.Transform", lambda: Body.Transform),                       # noqa
            ("Midsurface.Convert", lambda: Midsurface.Convert),               # noqa
            ("Body.Volume", lambda: Body.Volume),                             # noqa
            ("DesignBody.Delete", lambda: DesignBody.Delete),                 # noqa
            ("BodySelection.Create", lambda: BodySelection.Create),           # noqa
            ("ViewHelper.SetObjectVisibility", lambda: ViewHelper.SetObjectVisibility),  # noqa
            ("VisibilityType.Hide", lambda: VisibilityType.Hide),             # noqa
        ]
        for nm, fn in checks:
            try:
                fn()
                print("  [ok]   %s" % nm)
            except Exception as ex:
                print("  [нет]  %s  (%s)" % (nm, ex))

    @staticmethod
    def create_beam(part, p_start, p_end, section_curves, name):
        """Создать балку вдоль оси по профилю сечения.  [TODO]"""
        raise NotImplementedError("create_beam")

    @staticmethod
    def end_section_face(body, axis):
        """Торцевая грань тела, нормаль которой || axis (для экструзии в балку).  [TODO]"""
        best = None
        for f, a, n, o in SC.planar_faces(body):
            d = abs(n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2])
            if d > 0.99 and (best is None or a > best[1]):
                best = (f, a, n, o)
        return best

    @staticmethod
    def create_tracked(part, create_fn, name):
        """Выполнить создание и вернуть НОВОЕ тело по ссылке на объект, а не по имени.
        Автонумерация (Surface1, Surface2, ...) скрипту не нужна: результат берём
        как разницу множеств part.Bodies до/после вызова, сразу переименовываем
        в детерминированное имя и храним ссылку в Item.result.  [TODO] проверить
        что DesignBody корректно хешируется/сравнивается в IronPython; иначе
        сравнивать по списку индексов."""
        before = set(part.Bodies)
        ret = create_fn()
        if ret is not None and hasattr(ret, "Name"):     # если API вернул объект - берём его
            new = ret
        else:
            added = [b for b in part.Bodies if b not in before]
            new = added[0] if len(added) == 1 else None  # >1 или 0 -> ошибка, пометим в note
        if new is not None:
            SC.rename(new, name)
        return new

    @staticmethod
    def rename(body, name):
        try:
            body.Name = name
        except Exception:
            pass

    @staticmethod
    def extend_surface(face_or_edge, distance):
        """Продлить поверхность/кромку.  [TODO]  (Extend / Fill / Merge в SpaceClaim)"""
        raise NotImplementedError("extend_surface")


# =============================================================================
# 2. ВЕКТОРНЫЕ УТИЛИТЫ
# =============================================================================
def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(a):
    l = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / l, a[1] / l, a[2] / l)


# =============================================================================
# 3. МОДЕЛЬ ДАННЫХ
# =============================================================================
class Abort(Exception):
    """Аварийная остановка этапа (аномалия при создании тел)."""
    pass


class Item(object):
    """Результат классификации одного тела."""
    SHEET, STIFFENER, SMALL, SKIP = "SHEET", "STIFFENER", "SMALL", "SKIP"

    def __init__(self, body):
        self.body = body
        self.kind = Item.SKIP
        self.src_name = getattr(body, "Name", "?")   # имя исходного тела (для отчёта)
        self.name = self.src_name                    # новое имя результата
        self.idx = 0                                 # сквозной индекс в списке (уникальность)
        self.thickness = None       # м
        self.t_dir = None           # единичный вектор толщины
        self.axis = None            # для профиля: ось
        self.length = None
        self.offset = Cfg.DEFAULT_OFFSET
        self.n_out = None           # внешняя нормаль копируемой грани
        self.curved = False         # гнутый лист (наибольшая грань неплоская)
        self.faces = None           # грани одной стороны для копирования (гнутый лист)
        self.result = None          # созданное тело (surface/beam)
        self.note = ""


# =============================================================================
# 4. КЛАССИФИКАЦИЯ
# =============================================================================
def sorted_dims(body):
    return sorted(SC.bbox_size(body))


def sheet_thickness_dir(body):
    """Направление и толщина листа по двум самым большим параллельным плоским граням.
    Устойчиво к наклонным листам (в отличие от осевого bbox).
    Возвращает (t, unit_dir) или None, если плоских граней нет (гнутый лист -> [TODO])."""
    faces = SC.planar_faces(body)
    if not faces:
        return None
    faces.sort(key=lambda x: -x[1])
    n = norm(faces[0][2])
    return (SC.extent_along(body, n), n)


def classify(body):
    it = Item(body)
    d = sorted_dims(body)                    # d[0] <= d[1] <= d[2]
    if d[0] <= 0:
        it.note = "нулевой габарит"
        return it

    fa = SC.faces_by_area(body)
    vol = SC.volume(body)
    mm = SC_UNIT()

    # признак 1: один габарит << двух других (плоский лист вдоль осей)
    bbox_sheet = d[1] / d[0] >= Cfg.SHEET_RATIO and d[0] * mm <= Cfg.SHEET_MAX_T
    # признак 2 (гнутые/составные/наклонные листы): тело = две стороны + торцы,
    # толщина t = V / A_ср_сторон мала относительно размера стороны
    thin, sides = None, SC.sheet_sides(fa, vol)
    if sides:
        t_va, big, small = sides
        a_big = sum(x[1] for x in big)
        if t_va > 0 and math.sqrt(a_big) / t_va >= Cfg.SHEET_RATIO and t_va * mm <= Cfg.SHEET_MAX_T:
            thin = t_va
    is_stiff = (d[2] / max(d[1], 1e-9) >= Cfg.STIFF_ASPECT_MIN and d[1] <= Cfg.STIFF_MAX_SECTION)

    if bbox_sheet or (thin and not is_stiff):
        it.kind = Item.SHEET
        if sides:
            grp = sides[1] if Cfg.CURVED_SIDE == "LARGER" else sides[2]
            # составной (несколько граней на сторону) или неплоский лист
            if len(grp) > 1 or not all(x[2] for x in grp):
                it.curved, it.faces = True, [x[0] for x in grp]
                it.thickness = sides[0]
                return it
        elif fa and not fa[0][2]:               # стороны не разобраны, грань неплоская
            it.curved, it.faces = True, [fa[0][0]]
            it.thickness = d[0]
            it.note = "стороны листа не разобраны: скопирована 1 грань, толщина по bbox"
            return it
        td = sheet_thickness_dir(body)
        if td:
            it.thickness, it.t_dir = td[0], td[1]
        else:
            it.thickness = thin or d[0]
            it.note = "нет плоских граней: толщина оценочная"
        return it

    # --- профиль (полособульб): два малых размера, один длинный ---
    if is_stiff:
        it.kind, it.length = Item.STIFFENER, d[2]
        it.axis = longest_axis(body)         # [TODO] для наклонных - по граням
        return it

    # --- малая связь (кница и пр.) ---
    it.kind = Item.SMALL
    return it


def SC_UNIT():
    return Cfg.UNIT_TO_MM


def longest_axis(body):
    """Ось профиля. Простая версия: наибольший габарит bbox.
    [TODO] для наклонных профилей: ось нормальна к торцевой грани/по экструзии."""
    s = SC.bbox_size(body)
    i = s.index(max(s))
    v = [0.0, 0.0, 0.0]
    v[i] = 1.0
    return tuple(v)


# =============================================================================
# 5. ЭТАП 1 - ЛИСТЫ
# =============================================================================
def pick_sheet_face(it):
    """Выбрать грань для копирования и определить, откуда откладывать толщину.
    Возвращает DesignFace или None. Заполняет it.n_out (внешняя нормаль грани).
    Если нормаль/команда выкладки не найдена -> BOT/MID/TOP по Cfg."""
    faces = SC.planar_faces(it.body)
    if not faces or it.t_dir is None:
        it.offset = Cfg.DEFAULT_OFFSET
        return None
    # две самые большие грани листа (противоположные стороны)
    pair = sorted(faces, key=lambda x: -x[1])[:2]
    side = Cfg.COPY_SIDE
    if side == "MID":
        it.n_out = pair[0][2]
        return pair[0][0]
    ref = norm(Cfg.REF_AXIS)
    pair.sort(key=lambda x: dot(x[2], ref))          # [0]=BOT, [1]=TOP
    chosen = pair[1] if side == "TOP" else pair[0]
    it.n_out = chosen[2]
    return chosen[0]


def offset_for(n_surface, n_out):
    """Сторона выкладки для Mechanical (Offset Type).
    Материал листа лежит в направлении -n_out (вглубь от копируемой грани).
    Если нормаль созданной поверхности смотрит наружу (вдоль n_out), материал
    с её отрицательной стороны -> "TOP" (поверхность = верх оболочки, толщина вниз).
    Иначе -> "BOT".  [TODO] подтвердить знак на одном тестовом листе в Mechanical."""
    return "TOP" if dot(n_surface, n_out) > 0 else "BOT"


def _flush():
    try:
        import sys
        sys.stdout.flush()
    except Exception:
        pass


def process_sheets(items):
    todo = [i for i in items if i.kind == Item.SHEET]
    if Cfg.LIMIT is not None:
        todo = todo[:Cfg.LIMIT]
    print("Листов к обработке: %d" % len(todo))
    part0 = SC.root()
    start_count = len(list(part0.Bodies))
    for k, it in enumerate(todo):
        print("  [%d/%d] %s" % (k + 1, len(todo), it.src_name))
        _flush()
        try:
            _process_one_sheet(it)
        except Abort:
            raise
        except Exception as ex:
            _reraise_abort(ex)
            it.note += " ОШИБКА: %s" % ex
        print("      -> %s | %s" % (it.name, it.note.strip() or "ok"))
        _flush()
        # предохранитель: на лист допускается не более +2 тела (копия + результат)
        now = len(list(part0.Bodies))
        if now > start_count + 2 * (k + 1):
            raise Abort("число тел выросло с %d до %d (ожидалось <= %d). Ctrl+Z."
                        % (start_count, now, start_count + 2 * (k + 1)))
        if it.result is not None and not Cfg.DRY_RUN:
            # результат проверен -> убираем исходный солид
            ok = SC.dispose_original(it.body)
            if not ok:
                it.note += " исходник не обработан (%s)" % Cfg.ORIGINAL_ACTION
            if not (ok and Cfg.ORIGINAL_ACTION == "DELETE"):
                SC.rename(it.body, "ORIG_" + it.src_name)   # чтобы не обработать повторно


def _sheet_by_midsurface(it):
    it.offset = "MID"
    t = it.thickness
    it.name = "SHEET_%04d_t%.1f_MID" % (it.idx, t * SC_UNIT())
    if Cfg.DRY_RUN:
        return
    part = SC.part_of(it.body)
    # запас 10% на верхнюю границу толщины для Midsurface.Convert
    it.result = SC.midsurface_convert(part, it.body, it.name, t * 1.1)
    if it.result is None:
        it.note += " Midsurface.Convert не дала поверхность"
        if Cfg.FALLBACK_TO_FACE:
            it.note += "; построено копированием грани"
            return _process_one_sheet(it, allow_midsurface=False)
        return
    SC.rename(it.result, it.name)
    ok, it.result = SC.set_thickness(it.result, t, "MID")
    if not ok:
        it.note += " толщина не задана (см. лог)"


def _sheet_curved(it):
    """Гнутый/составной лист: копия всех граней одной стороны + толщина с выкладкой."""
    t = it.thickness
    it.offset = Cfg.CURVED_OFFSET
    it.name = "SHEET_%04d_t%.1f_%s" % (it.idx, t * SC_UNIT(), it.offset)
    if Cfg.DRY_RUN:
        return
    part = SC.part_of(it.body)
    it.result = SC.copy_face_generic(part, it.body, it.faces, it.name)
    ok, it.result = SC.set_thickness(it.result, t, it.offset)
    if not ok:
        it.note += " толщина не задана (см. лог)"


def _process_one_sheet(it, allow_midsurface=True):
    if it.curved:
        return _sheet_curved(it)
    if allow_midsurface and Cfg.COPY_SIDE == "MID" and Cfg.USE_MIDSURFACE:
        return _sheet_by_midsurface(it)
    face = pick_sheet_face(it)
    if face is None:
        it.note += " нет плоской грани: пропущен [TODO гнутые листы]"
        return
    t = it.thickness
    mid = (Cfg.COPY_SIDE == "MID")
    shift = None
    if mid:   # срединная поверхность: сдвиг копии на t/2 вглубь листа
        shift = (-it.n_out[0] * t / 2.0, -it.n_out[1] * t / 2.0, -it.n_out[2] * t / 2.0)
    it.offset = "MID" if mid else "?"
    it.name = "SHEET_%04d_t%.1f_%s" % (it.idx, t * SC_UNIT(), it.offset)  # предв. имя
    if Cfg.DRY_RUN:
        return

    part = SC.part_of(it.body)
    res = SC.create_tracked(part, lambda: SC.copy_face_to_surface(part, face, it.name, shift)[0], it.name)
    if res is None:
        it.note += " результат не получен"
        return
    it.result = res
    n_surf = SC.face_normal(list(res.Faces)[0].Shape)
    if not mid:
        it.offset = offset_for(n_surf, it.n_out)
    it.name = "SHEET_%04d_t%.1f_%s" % (it.idx, t * SC_UNIT(), it.offset)
    SC.rename(res, it.name)
    ok, it.result = SC.set_thickness(res, t, it.offset)
    if not ok:
        it.note += " толщина не задана (см. лог)"


# =============================================================================
# 6. ЭТАП 2 - ПОЛОСОБУЛЬБЫ
# =============================================================================
def process_stiffeners(items):
    for it in [i for i in items if i.kind == Item.STIFFENER]:
        it.name = "BEAM_%04d_L%.0f" % (it.idx, it.length * SC_UNIT())
        if Cfg.DRY_RUN:
            continue
        _beam_from_section(it)


def _beam_from_section(it):
    """Балка по экструзии: берём торцевое сечение тела, строим beam вдоль оси.
    Сечение = реальная геометрия торца (не упрощаем, не подбираем эквиваленты)."""
    sec = SC.end_section_face(it.body, it.axis)
    if not sec:
        it.note = "нет торцевой грани"
        return
    # [TODO] p_start/p_end: центр торца и центр противоположного торца
    # [TODO] BEAM_OFFSET_TO_PLATE: по умолчанию ось балки идёт через ц.т. сечения,
    #        а нужен контакт по поверхности обшивки. Варианты:
    #          1) сместить ось на (расстояние ц.т. -> плоскость привязки обшивки);
    #          2) задать offset профиля (Cross-section offset) в SpaceClaim;
    #          3) оставить ц.т. и ввести offset на стороне Mechanical (beam offset).
    it.result = SC.create_tracked(
        SC.root(), lambda: SC.create_beam(SC.root(), None, None, sec, it.name), it.name)


# =============================================================================
# 7. ЭТАП 3 - КНИЦЫ И МЕЛКИЕ СВЯЗИ
# =============================================================================
def process_small(items):
    """Кницы -> поверхности. По сути тот же путь, что и листы
    (толщина = наименьший габарит, поверхность = срединная/выбранная)."""
    for it in [i for i in items if i.kind == Item.SMALL]:
        td = sheet_thickness_dir(it.body)
        if td:
            it.thickness, it.t_dir = td
        it.name = "SMALL_%04d" % it.idx
        if Cfg.DRY_RUN:
            continue
        # [TODO] переиспользовать pick_sheet_face + copy_face_to_surface;
        #        кницы сложной формы (не листовые) -> оставить solid/локализация.


# =============================================================================
# 8. ЭТАП 4 - ПРОДЛЕНИЕ И ЗАЗОРЫ
# =============================================================================
def heal_gaps(items):
    """После устаканивания основных связей:
      a) найти пары кромок/поверхностей на расстоянии <= GAP_TOL;
      b) продлить (Extend) на величину <= EXTEND_MAX до пересечения;
      c) сшить (Merge) / Fill по необходимости.
    Идея реализации [TODO]:
      - собрать все свободные кромки поверхностных тел (edge с 1 гранью);
      - для каждой кромки искать ближайшую поверхность другого тела
        (расстояние/проекция) -> кандидаты на продление;
      - группировать по компонентам, не склеивать shell с beam/solid (разная топология).
    """
    if Cfg.DRY_RUN:
        return


# =============================================================================
# 9. ОТЧЁТ
# =============================================================================
def _report_path():
    """Папку по умолчанию (cwd = каталог установки SpaceClaim) писать нельзя ->
    пробуем OUT_DIR, Документы, temp; берём первую, где реально создаётся файл."""
    import tempfile
    import System
    try:
        docs = System.Environment.GetFolderPath(System.Environment.SpecialFolder.MyDocuments)
    except Exception:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
    cands = [Cfg.OUT_DIR, docs, tempfile.gettempdir()]
    for d in cands:
        if not d:
            continue
        try:
            path = os.path.join(d, Cfg.REPORT_NAME)
            open(path, "a").close()
            return path
        except Exception:
            continue
    return None


def write_report(items):
    path = _report_path()
    if path is None:
        print("Отчёт не записан: нет доступной папки (задайте Cfg.OUT_DIR)")
        return None
    def _b(x):
        # IronPython 2: пишем байты UTF-8, иначе кириллица в note даёт ошибку кодировки
        try:
            if isinstance(x, unicode):  # noqa
                return x.encode("utf-8")
            return str(x)
        except Exception:
            return repr(x)
    with open(path, "wb") as f:
        f.write(_b("new_name;src_name;kind;thickness_mm;offset;length_mm;note\n"))
        for it in items:
            line = "%s;%s;%s;%s;%s;%s;%s\n" % (
                it.name, it.src_name, it.kind,
                "%.2f" % (it.thickness * SC_UNIT()) if it.thickness else "",
                it.offset if it.kind == Item.SHEET else "",
                "%.0f" % (it.length * SC_UNIT()) if it.length else "",
                it.note)
            f.write(_b(line))
    return path


# =============================================================================
# 10. MAIN
# =============================================================================
def main():
    bodies = SC.selected_bodies() if Cfg.ONLY_SELECTED else None
    if bodies is None:
        bodies = SC.design_bodies(SC.root())
    # уже обработанные/созданные скриптом тела не трогаем (защита от повторной обработки)
    skipped = [str(getattr(b, "Name", "")) for b in bodies
               if str(getattr(b, "Name", "")).startswith(Cfg.SKIP_PREFIXES)]
    bodies = [b for b in bodies
              if not str(getattr(b, "Name", "")).startswith(Cfg.SKIP_PREFIXES)]
    print("Тел в обработке: %d" % len(bodies))
    if skipped:
        print("Пропущено как уже обработанные (префиксы %s): %s" % (
            "/".join(Cfg.SKIP_PREFIXES), ", ".join(skipped[:20])))
    if not bodies:
        print("Нечего обрабатывать: верните исходный солид (Ctrl+Z) или переименуйте "
              "его, убрав префикс ORIG_, и удалите прошлую поверхность SHEET_.")
    items = []
    for n, b in enumerate(bodies):
        print("  классификация [%d/%d] %s (граней: %s)" % (
            n + 1, len(bodies), getattr(b, "Name", "?"), SC._safe_len(lambda: b.Faces)))
        _flush()
        items.append(classify(b))
    for n, it in enumerate(items):
        it.idx = n                                   # имена результатов уникальны по idx

    if Cfg.PROBE_API:
        print("Проверка API:")
        SC.probe()
    st = Cfg.STAGES
    if st.get("sheets"):
        process_sheets(items)        # 1
    if st.get("stiffeners"):
        process_stiffeners(items)    # 2
    if st.get("small"):
        process_small(items)         # 3
    if st.get("healing"):
        heal_gaps(items)             # 4

    try:
        path = write_report(items)
    except Exception as ex:
        path = None
        print("Ошибка записи отчёта: %s" % ex)
    cnt = {}
    for it in items:
        cnt[it.kind] = cnt.get(it.kind, 0) + 1
    print("Итого: %s | DRY_RUN=%s | отчёт: %s" % (cnt, Cfg.DRY_RUN, path))


class _Tee(object):
    """Дублирует весь вывод консоли в файл idealize_log.txt (рядом с отчётом)."""
    def __init__(self, stream, path):
        self.stream, self.f = stream, open(path, "wb")

    def write(self, x):
        try:
            self.stream.write(x)
        except Exception:
            pass
        try:
            self.f.write(x.encode("utf-8") if isinstance(x, unicode) else x)  # noqa
            self.f.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


import sys as _sys
_old_out = _sys.stdout
try:
    _rp = _report_path()
    if _rp:
        _log_path = os.path.join(os.path.dirname(_rp), "idealize_log.txt")
        _sys.stdout = _Tee(_old_out, _log_path)
        print("Лог: %s" % _log_path)
except Exception:
    pass

try:
    main()
except Abort as ex:
    print("ОСТАНОВЛЕНО: %s" % ex)
except Exception:
    import traceback
    print(traceback.format_exc())
finally:
    try:
        if isinstance(_sys.stdout, _Tee):
            _sys.stdout.f.close()
        _sys.stdout = _old_out
    except Exception:
        pass
