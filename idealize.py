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

clr.AddReference("System.Core")
from System import Math as _Math  # noqa  (резерв)

# SpaceClaim API: глобальные объекты (GetRootPart, Window, Selection ...)
# доступны в скрипте автоматически. Версию V* выбирайте по вашей сборке.
try:
    from SpaceClaim.Api.V19 import *  # noqa
except Exception:
    pass


# =============================================================================
# 0. КОНФИГУРАЦИЯ
# =============================================================================
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
    BEAM_MODE = "BEAM_FROM_SECTION"  # "BEAM_FROM_SECTION" | "EQUIV_FLAT_BAR"
    BEAM_OFFSET_TO_PLATE = True      # [TODO] сдвиг оси балки на поверхность обшивки
    EQUIV_TABLE_XLSX = None          # путь к Excel-таблице подбора (пришлёте позже)

    # --- малые связи ---
    SMALL_MAX_AREA = 0.5             # м^2, всё что мельче и не лист/профиль -> кница

    # --- зазоры ---
    GAP_TOL = 0.010                  # м, закрывать зазоры <= 10 мм
    EXTEND_MAX = 0.050               # м, макс. величина продления

    # --- вывод ---
    OUT_DIR = None                   # None -> папка скрипта / рабочая
    REPORT_NAME = "idealize_report.csv"
    DRY_RUN = True                   # True: только классификация и отчёт, без правок


# =============================================================================
# 1. SC API ADAPTER  (единственное место с вызовами SpaceClaim API)
#    Все функции ниже [TODO: проверить на вашей версии]
# =============================================================================
class SC(object):
    @staticmethod
    def root():
        return GetRootPart()  # noqa

    @staticmethod
    def design_bodies(part):
        """Все твердые тела (DesignBody) корневого узла, рекурсивно по компонентам."""
        out = []
        for b in part.Bodies:
            out.append(b)
        for comp in part.Components:
            out.extend(SC.design_bodies(comp.Template))
        return out

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
    def planar_faces(body):
        """[(face, area, unit_normal(tuple), origin Point)] только плоские грани."""
        res = []
        for f in body.Faces:
            try:
                surf = f.Shape.Geometry
                if isinstance(surf, Plane):  # noqa
                    n = surf.Frame.DirZ  # [TODO] учесть f.Shape.IsReversed
                    if f.Shape.IsReversed:
                        n = -n
                    res.append((f, f.Shape.Area, (n.X, n.Y, n.Z), surf.Frame.Origin))
            except Exception:
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
    def copy_face_to_surface(part, face, name):
        """Скопировать грань в новое поверхностное тело. Возвращает DesignBody|None.  [TODO]
        Варианты реализации (проверить какой работает у вас):
          a) Body.CreateSurfaceBody + Face.Shape.Geometry/Edges
          b) Copy.Execute(Selection.Create(face)) + Paste
          c) ExtrudeFaces / Offset face на 0 с опцией "copy"
        """
        raise NotImplementedError("copy_face_to_surface: подберите вариант a/b/c")

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
class Item(object):
    """Результат классификации одного тела."""
    SHEET, STIFFENER, SMALL, SKIP = "SHEET", "STIFFENER", "SMALL", "SKIP"

    def __init__(self, body):
        self.body = body
        self.kind = Item.SKIP
        self.name = getattr(body, "Name", "?")
        self.thickness = None       # м
        self.t_dir = None           # единичный вектор толщины
        self.axis = None            # для профиля: ось
        self.length = None
        self.offset = Cfg.DEFAULT_OFFSET
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

    # --- лист: один размер << двух других ---
    if d[1] / d[0] >= Cfg.SHEET_RATIO and d[0] * SC_UNIT() <= Cfg.SHEET_MAX_T:
        td = sheet_thickness_dir(body)
        if td:
            it.kind, it.thickness, it.t_dir = Item.SHEET, td[0], td[1]
        else:
            it.kind, it.thickness = Item.SHEET, d[0]
            it.note = "нет плоских граней: толщина по bbox [TODO]"
        return it

    # --- профиль (полособульб): два малых размера, один длинный ---
    if d[2] / max(d[1], 1e-9) >= Cfg.STIFF_ASPECT_MIN and d[1] <= Cfg.STIFF_MAX_SECTION:
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
    """Выбрать грань для копирования и определить offset.
    Если нормаль/команда выкладки не найдена -> BOT/MID/TOP по Cfg."""
    faces = SC.planar_faces(it.body)
    if not faces or it.t_dir is None:
        it.offset = Cfg.DEFAULT_OFFSET
        return None
    pair = sorted(faces, key=lambda x: -x[1])[:2]    # две большие грани
    side = Cfg.COPY_SIDE
    if side == "MID":
        it.offset = "MID"
        return pair[0][0]                            # [TODO] реальная срединная
    ref = norm(Cfg.REF_AXIS)
    pair.sort(key=lambda x: dot(x[2], ref))          # [0]=BOT, [1]=TOP
    face = pair[1] if side == "TOP" else pair[0]
    it.offset = side
    return face[0]


def process_sheets(items):
    for it in [i for i in items if i.kind == Item.SHEET]:
        face = pick_sheet_face(it)
        it.name = "SHEET_t%.1f_%s_%s" % (it.thickness * SC_UNIT(), it.offset, it.name)
        if Cfg.DRY_RUN or face is None:
            continue
        # Поверхность копируется с той грани, откуда откладывается толщина
        # (offset в ANSYS: TOP/BOT/MID относительно нормали поверхности).
        it.result = SC.copy_face_to_surface(SC.root(), face, it.name)
        # [TODO] при COPY_SIDE != MID проверить ориентацию нормали поверхности:
        #        нормаль должна смотреть в сторону, где была толщина листа.


# =============================================================================
# 6. ЭТАП 2 - ПОЛОСОБУЛЬБЫ
# =============================================================================
def process_stiffeners(items):
    for it in [i for i in items if i.kind == Item.STIFFENER]:
        it.name = "BEAM_L%.0f_%s" % (it.length * SC_UNIT(), it.name)
        if Cfg.DRY_RUN:
            continue
        if Cfg.BEAM_MODE == "BEAM_FROM_SECTION":
            _beam_from_section(it)
        else:
            _equiv_flat_bar(it)


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
    it.result = SC.create_beam(SC.root(), None, None, sec, it.name)


def _equiv_flat_bar(it):
    """Полоса с эквивалентным моментом сопротивления по таблице Excel.  [TODO]
    Вход: тип/размер полособульба -> таблица -> (h, t) эквивалентной полосы."""
    it.note = "EQUIV_FLAT_BAR: ждём таблицу Excel"


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
        it.name = "SMALL_%s" % it.name
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
def write_report(items):
    out_dir = Cfg.OUT_DIR or os.getcwd()
    path = os.path.join(out_dir, Cfg.REPORT_NAME)
    with open(path, "w") as f:
        f.write("name;kind;thickness_mm;offset;length_mm;note\n")
        for it in items:
            f.write("%s;%s;%s;%s;%s;%s\n" % (
                it.name, it.kind,
                "%.2f" % (it.thickness * SC_UNIT()) if it.thickness else "",
                it.offset if it.kind == Item.SHEET else "",
                "%.0f" % (it.length * SC_UNIT()) if it.length else "",
                it.note))
    return path


# =============================================================================
# 10. MAIN
# =============================================================================
def main():
    bodies = SC.design_bodies(SC.root())
    items = [classify(b) for b in bodies]

    process_sheets(items)        # 1
    process_stiffeners(items)    # 2
    process_small(items)         # 3
    heal_gaps(items)             # 4

    path = write_report(items)
    cnt = {}
    for it in items:
        cnt[it.kind] = cnt.get(it.kind, 0) + 1
    print("Итого: %s | DRY_RUN=%s | отчёт: %s" % (cnt, Cfg.DRY_RUN, path))


main()
