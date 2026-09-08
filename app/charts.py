"""Private, deterministic chart cards built from exact SQLite aggregates.

Call ``build_chart_data`` with the server-resolved active budget ID, never an ID
supplied by a callback. Render with ``asyncio.to_thread`` to keep polling responsive.
No transaction descriptions, member names, or identifiers enter the PNG.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import textwrap
from threading import Lock

import aiosqlite

from .inputs import month_label, valid_month


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    amount_minor: int


@dataclass(frozen=True)
class ChartData:
    month: str
    as_of: date
    elapsed_days: int
    month_days: int
    income_minor: int
    expense_minor: int
    categories: tuple[CategoryTotal, ...]
    daily_minor: tuple[int, ...]


async def build_chart_data(db, budget_id: int, month: str, as_of: date) -> ChartData:
    """Snapshot all recorded amounts in this budget, through the given local date.

    Current months stop at as_of; historical months cover every day; future
    months contain no actuals. SQL aggregation does not impose a transaction
    pagination cap. At most six category rows and 31 daily values are retained.
    A negative budget ID is a family budget and follows the same isolation rule.
    """
    month = valid_month(month)
    year, number = map(int, month.split('-'))
    month_days = calendar.monthrange(year, number)[1]
    current_month = f'{as_of.year:04d}-{as_of.month:02d}'
    elapsed = month_days if month < current_month else as_of.day if month == current_month else 0
    if not elapsed:
        return ChartData(month, as_of, 0, month_days, 0, 0, (), ())
    params = (budget_id, month + '-01', f'{month}-{elapsed:02d}')
    where = 'user_id=? AND occurred_on BETWEEN ? AND ?'
    async with aiosqlite.connect(db.path) as conn:
        # Keep totals, distribution, and trend mutually consistent while other
        # users or family members add/edit operations on another connection.
        await conn.execute('BEGIN')
        totals = await (await conn.execute(
            'SELECT COALESCE(SUM(CASE WHEN kind=\'income\' THEN amount_minor ELSE 0 END),0), '
            'COALESCE(SUM(CASE WHEN kind=\'expense\' THEN amount_minor ELSE 0 END),0) '
            f'FROM transactions WHERE {where}', params,
        )).fetchone()
        categories = await (await conn.execute(
            f'SELECT category,SUM(amount_minor) AS spent FROM transactions WHERE {where} '
            "AND kind='expense' GROUP BY category ORDER BY spent DESC,category ASC LIMIT 5", params,
        )).fetchall()
        daily = await (await conn.execute(
            f'SELECT occurred_on,SUM(amount_minor) FROM transactions WHERE {where} '
            "AND kind='expense' GROUP BY occurred_on ORDER BY occurred_on", params,
        )).fetchall()
    income_minor, expense_minor = map(int, totals)
    grouped = [CategoryTotal(name, int(amount)) for name, amount in categories]
    remainder = expense_minor - sum(row.amount_minor for row in grouped)
    if remainder:
        grouped.append(CategoryTotal('Остальные категории', remainder))
    by_day = {int(day[-2:]): int(amount) for day, amount in daily}
    return ChartData(month, as_of, elapsed, month_days, income_minor, expense_minor,
                     tuple(grouped), tuple(by_day.get(day, 0) for day in range(1, elapsed + 1)))


_RENDER_LOCK = Lock()
_NAVY = '#0C1625'
_PANEL = '#132235'
_WHITE = '#F3F7FC'
_MUTED = '#91A5BD'
_MINT = '#8CF0CD'
_PALETTE = (_MINT, '#95BCFF', '#CEABFF', '#FFD28F', '#FAAFB8', '#607A97')


def _money(minor: int) -> str:
    whole, cents = divmod(abs(minor), 100)
    value = f'{whole:,}'.replace(',', ' ')
    return ('−' if minor < 0 else '') + value + (f',{cents:02d}' if cents else '') + ' ₽'


def _percent(part: int, total: int) -> str:
    value = (Decimal(part) * 100 / Decimal(total)).quantize(Decimal('.1'), rounding=ROUND_HALF_UP)
    return str(value).replace('.', ',') + '%'


def _compact_rubles(value: float) -> str:
    for scale, suffix in ((1e12, 'трлн'), (1e9, 'млрд'), (1e6, 'млн'), (1e3, 'тыс.')):
        if abs(value) >= scale:
            return f'{value / scale:.1f}'.rstrip('0').rstrip('.').replace('.', ',') + ' ' + suffix
    return f'{value:.2f}'.rstrip('0').rstrip('.').replace('.', ',')


def render_expense_chart(data: ChartData, *, scope_label: str = 'Личный бюджет', example: bool = False) -> bytes:
    """Return a 1080 × 1440 PNG. Does not use pyplot, a GUI, network, or temp files.

    Matplotlib font/layout state is serialized to bound concurrent image memory.
    Values stay integer cents until plotted. Example data must set example=True.
    """
    # Lazy imports keep non-chart bot actions independent of Matplotlib startup.
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    with _RENDER_LOCK, rc_context({'font.family': 'DejaVu Sans', 'text.usetex': False,
                                  'text.parse_math': False, 'axes.unicode_minus': False}):
        fig = Figure(figsize=(9, 12), dpi=120, facecolor=_NAVY)
        FigureCanvasAgg(fig)

        def text(x, y, value, size=16, color=_WHITE, weight='normal', **kwargs):
            return fig.text(x, y, value, fontsize=size, color=color, fontweight=weight,
                            va='center', **kwargs)

        def panel(x, y, width, height):
            fig.add_artist(FancyBboxPatch((x, y), width, height, boxstyle='round,pad=0.008,rounding_size=0.020',
                                         transform=fig.transFigure, facecolor=_PANEL, linewidth=0, zorder=0))

        text(.065, .955, 'ТВОЙ БЮДЖЕТ', 15, _MINT, 'bold')
        if example:
            text(.935, .955, 'ПРИМЕР', 14, '#FFD28F', 'bold', ha='right')
        text(.065, .909, month_label(data.month), 32, weight='bold')
        scope = ' '.join(scope_label.split())[:64]
        # Scope is controlled by the integration (personal/family), never an
        # arbitrary member name. Even a long label cannot escape the card.
        text(.065, .867, textwrap.shorten(scope, width=43, placeholder='…'), 16, _MUTED)
        text(.065, .813, 'Расходы за период', 16, _MUTED)
        amount = _money(data.expense_minor)
        text(.065, .763, amount, min(43, 590 / max(1, len(amount))), _WHITE, 'bold')
        text(.065, .708, 'Доходы  ' + _money(data.income_minor), 15, _MINT)

        panel(.055, .340, .89, .322)
        text(.085, .634, 'Куда уходят деньги', 21, weight='bold')
        if data.expense_minor:
            for index, row in enumerate(data.categories):
                y = .590 - index * .043
                color = _PALETTE[index]
                label = ' '.join(row.category.split())
                label = label[0].upper() + label[1:] if label else 'Без категории'
                # Preserve custom category text; wrap long labels within their
                # column, leaving exact amounts and shares in a separate column.
                label_lines = textwrap.wrap(label, width=36) or ['Без категории']
                label_size = 14 if len(label_lines) == 1 else 10.7
                text(.085, y, '\n'.join(label_lines), label_size, linespacing=1.02)
                metric = _money(row.amount_minor) + ' · ' + _percent(row.amount_minor, data.expense_minor)
                text(.915, y, metric, min(13, 300 / max(1, len(metric))), _WHITE, ha='right')
                fig.add_artist(FancyBboxPatch((.085, y - .020), .83, .006,
                    boxstyle='round,pad=0,rounding_size=0.003', transform=fig.transFigure,
                    facecolor='#24364B', linewidth=0))
                fig.add_artist(FancyBboxPatch((.085, y - .020), .83 * row.amount_minor / data.expense_minor, .006,
                    boxstyle='round,pad=0,rounding_size=0.003', transform=fig.transFigure,
                    facecolor=color, linewidth=0))
        else:
            text(.5, .513, 'Пока без расходов', 23, weight='bold', ha='center')
            message = ('Этот месяц ещё не начался' if not data.elapsed_days else
                       'Добавь первую трату — здесь появится диаграмма')
            text(.5, .476, message, 13.5, _MUTED, ha='center')

        panel(.055, .077, .89, .231)
        text(.085, .278, 'Расходы по дням', 21, weight='bold')
        if data.expense_minor and data.elapsed_days:
            ax = fig.add_axes((.132, .124, .768, .117), facecolor=_PANEL)
            x = list(range(1, data.elapsed_days + 1))
            y = [value / 100 for value in data.daily_minor]
            ax.fill_between(x, y, color=_MINT, alpha=.09, linewidth=0)
            ax.plot(x, y, color=_MINT, linewidth=2.6, marker='o', markersize=3.4, clip_on=False)
            # The line ends at the last elapsed day; future days are blank,
            # never plotted as zero recorded expenditure.
            ax.set_xlim(.5, data.month_days + .5)
            ax.set_ylim(0, max(y) * 1.22)
            ticks = sorted({1, data.elapsed_days, data.month_days, *range(5, data.month_days, 5)})
            if data.elapsed_days not in (1, data.month_days):
                ticks = [day for day in ticks if day == data.elapsed_days or abs(day - data.elapsed_days) >= 2]
            ax.set_xticks(ticks)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3, min_n_ticks=2))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _compact_rubles(v)))
            ax.tick_params(axis='both', colors=_MUTED, labelsize=11, length=0, pad=7)
            ax.grid(axis='y', color='#2B3C51', alpha=.7, linewidth=.7)
            ax.set_axisbelow(True)
            for spine in ax.spines.values():
                spine.set_visible(False)
            text(.132, .255, '₽', 11, _MUTED)
            text(.9, .096, 'День месяца', 11, _MUTED, ha='right')
        else:
            text(.5, .195, 'График появится после первой траты', 15, _MUTED, ha='center')
        period = (f'01–{data.elapsed_days:02d}.{data.month[5:]}.{data.month[:4]}'
                  if data.elapsed_days else 'Будущий период')
        footer = 'Пример • вымышленные данные' if example else 'По внесённым операциям • ' + period
        text(.065, .036, footer, 12, _MUTED)
        try:
            output = BytesIO()
            fig.savefig(output, format='png', dpi=120, facecolor=_NAVY, metadata={'Software': 'Finance Bot'})
            return output.getvalue()
        finally:
            fig.clear()
