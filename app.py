import csv
import io
import os
import sqlite3
from datetime import date, timedelta
from functools import wraps
from collections import defaultdict

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-secret-key")

DATABASE = os.environ.get("DATABASE_PATH", "cgmbet.db")

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")

STRATEGIES = ["GG", "Over 2.5", "Over 1.5"]


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            match_date TEXT,
            match_time TEXT,
            championship TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            market TEXT,
            odd REAL DEFAULT 0,
            elo_gap TEXT DEFAULT '',
            gg_home TEXT DEFAULT '',
            gg_away TEXT DEFAULT '',
            over_home TEXT DEFAULT '',
            over_away TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def parse_float(value):
    if value is None:
        return 0
    value = str(value).replace(",", ".").strip()
    try:
        return float(value)
    except ValueError:
        return 0


def pick(row, names):
    """
    Cerca nei campi del CSV in modo flessibile.
    Gestisce colonne con parentesi graffe tipo {ELO GAP}, {QUOTE}, ecc.
    """
    # Normalizza le chiavi: rimuove spazi, parentesi graffe, lowercase
    def normalize(k):
        return str(k).strip().lower().replace("{", "").replace("}", "").strip()

    normalized = {normalize(k): v for k, v in row.items()}

    for wanted in names:
        wanted_norm = normalize(wanted)
        # Corrispondenza esatta
        if wanted_norm in normalized:
            val = normalized[wanted_norm]
            return str(val or "").strip().replace('"', "").strip()
        # Corrispondenza parziale
        for key, value in normalized.items():
            if wanted_norm in key or key in wanted_norm:
                return str(value or "").strip().replace('"', "").strip()
    return ""


def detect_delimiter(text):
    first = text.splitlines()[0] if text.splitlines() else ""
    return ";" if first.count(";") >= first.count(",") else ","


def odd_for_strategy(row, strategy):
    """
    Legge la quota reale dal CSV CGMBet.
    La colonna si chiama {QUOTE} in tutti gli slot.
    """
    return pick(row, ["{QUOTE}", "QUOTE", "quota", "odd", "coeff"])


def home_stat_for_strategy(row, strategy):
    """Legge la % statistica casa in base alla strategia."""
    if strategy == "GG":
        return pick(row, ["{gg casa}", "{GG Casa}", "gg casa", "gg home", "GG Casa10"])
    elif strategy == "Over 2.5":
        return pick(row, ["{over 2.5 casa}", "{Over 2.5 Casa}", "over 2.5 casa", "over25 casa", "Over25Casa10"])
    else:  # Over 1.5
        return pick(row, ["{over 1.5 casa}", "{Over 1.5 Casa}", "over 1.5 casa", "over15 casa", "Over15Casa10"])


def away_stat_for_strategy(row, strategy):
    """Legge la % statistica trasferta in base alla strategia."""
    if strategy == "GG":
        return pick(row, ["{gg trasferta}", "{GG Trasfe}", "gg trasferta", "gg away", "GG Trasf10"])
    elif strategy == "Over 2.5":
        return pick(row, ["{over 2.5 trasfe}", "{Over 2.5 Trasfe}", "over 2.5 trasferta", "over25 trasferta", "Over25Trasf10"])
    else:  # Over 1.5
        return pick(row, ["{over 1.5 trasfe}", "{Over 1.5 Trasfe}", "over 1.5 trasferta", "over15 trasferta", "Over15Trasf10"])


def get_counts(conn):
    total_all = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    gg_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'GG'").fetchone()[0]
    over25_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'Over 2.5'").fetchone()[0]
    over15_count = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy = 'Over 1.5'").fetchone()[0]
    return total_all, gg_count, over25_count, over15_count


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == APP_USERNAME and request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Credenziali non corrette.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    total_all, gg_count, over25_count, over15_count = get_counts(conn)

    strategy_counts = {"GG": gg_count, "Over 2.5": over25_count, "Over 1.5": over15_count}

    # Distribuzione quote per strategia
    odds_distribution = {}
    for s in STRATEGIES:
        rows = conn.execute("SELECT odd FROM matches WHERE strategy = ? AND odd > 0", (s,)).fetchall()
        low  = sum(1 for r in rows if r["odd"] < 1.40)
        mid  = sum(1 for r in rows if 1.40 <= r["odd"] <= 1.70)
        high = sum(1 for r in rows if r["odd"] > 1.70)
        odds_distribution[s] = {"low": low, "mid": mid, "high": high}

    # Andamento importazioni ultimi 14 giorni
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    imports_by_day = defaultdict(int)
    rows = conn.execute(
        "SELECT DATE(created_at) as day, COUNT(*) as cnt FROM matches GROUP BY DATE(created_at)"
    ).fetchall()
    for r in rows:
        imports_by_day[r["day"]] = r["cnt"]
    trend_labels = [d[5:] for d in days]
    trend_data   = [imports_by_day.get(d, 0) for d in days]

    today_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date = ?", (today.isoformat(),)
    ).fetchone()[0]
    next7_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=7)).isoformat())
    ).fetchone()[0]

    avg_odds = {}
    for s in STRATEGIES:
        avg = conn.execute(
            "SELECT AVG(odd) FROM matches WHERE strategy = ? AND odd > 0", (s,)
        ).fetchone()[0]
        avg_odds[s] = round(avg, 2) if avg else 0

    conn.close()

    return render_template(
        "dashboard.html",
        strategy_counts=strategy_counts,
        odds_distribution=odds_distribution,
        trend_labels=trend_labels,
        trend_data=trend_data,
        today_count=today_count,
        next7_count=next7_count,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
        avg_odds=avg_odds,
    )


@app.route("/")
@login_required
def index():
    strategy = request.args.get("strategy", "GG")
    search = request.args.get("search", "").strip()
    date_filter = request.args.get("date_filter", "")

    query = "SELECT * FROM matches WHERE strategy = ?"
    params = [strategy]

    if search:
        query += " AND (home_team LIKE ? OR away_team LIKE ? OR championship LIKE ? OR notes LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like, like])

    if date_filter == "today":
        query += " AND match_date = ?"
        params.append(date.today().isoformat())

    if date_filter == "7days":
        query += " AND match_date BETWEEN ? AND ?"
        params.append(date.today().isoformat())
        params.append((date.today() + timedelta(days=7)).isoformat())

    query += " ORDER BY match_date ASC, match_time ASC, championship ASC"

    conn = get_db()
    matches = conn.execute(query, params).fetchall()
    total_strategy = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE strategy = ?", (strategy,)
    ).fetchone()[0]
    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    conn.close()

    return render_template(
        "index.html",
        matches=matches,
        strategy=strategy,
        search=search,
        date_filter=date_filter,
        total=len(matches),
        total_strategy=total_strategy,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
    )


@app.route("/import", methods=["POST"])
@login_required
def import_csv():
    strategy = request.form.get("strategy", "GG")
    file = request.files.get("csv_file")

    if not file:
        flash("Nessun file caricato.", "error")
        return redirect(url_for("index", strategy=strategy))

    text = file.read().decode("utf-8-sig", errors="ignore")
    delimiter = detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    imported = 0
    conn = get_db()

    for row in reader:
        home = pick(row, ["Squadra Casa", "squadra casa", "casa", "home"])
        away = pick(row, ["Squadra Ospite", "squadra ospite", "ospite", "trasferta", "away"])

        if not home or not away:
            continue

        # Data/ora: il CSV CGMBet ha "Data/Ora" con formato "25/26 08/05/2026 1800"
        raw_datetime = pick(row, ["Data/Ora", "data/ora", "data", "date"])
        # Estrai data e ora dal formato "25/26 08/05/2026 1800"
        match_date = ""
        match_time = ""
        if raw_datetime:
            parts = raw_datetime.strip().split()
            # parts = ["25/26", "08/05/2026", "1800"]
            if len(parts) >= 3:
                match_date = parts[1]  # 08/05/2026
                t = parts[2]           # 1800
                match_time = t[:2] + ":" + t[2:] if len(t) == 4 else t
            elif len(parts) == 2:
                match_date = parts[0]
                match_time = parts[1]
            else:
                match_date = raw_datetime

        # Quota reale dalla colonna {QUOTE}
        odd_val = parse_float(odd_for_strategy(row, strategy))

        # Statistiche % casa e trasferta in base alla strategia
        home_stat = home_stat_for_strategy(row, strategy)
        away_stat = away_stat_for_strategy(row, strategy)

        # Salva in gg_home/gg_away per GG, in over_home/over_away per Over
        gg_home_val   = home_stat if strategy == "GG" else ""
        gg_away_val   = away_stat if strategy == "GG" else ""
        over_home_val = home_stat if strategy != "GG" else ""
        over_away_val = away_stat if strategy != "GG" else ""

        # Media gol (colonna {MEDIA GOAL})
        media_gol = pick(row, ["{MEDIA GOAL}", "MEDIA GOAL", "media goal", "media gol"])

        conn.execute(
            """
            INSERT INTO matches (
                strategy, match_date, match_time, championship,
                home_team, away_team, market, odd, elo_gap,
                gg_home, gg_away, over_home, over_away, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy,
                match_date,
                match_time,
                pick(row, ["Campionato", "campionato", "league", "lega"]),
                home,
                away,
                strategy,
                odd_val,
                pick(row, ["{ELO GAP}", "ELO GAP", "elo gap", "elo"]),
                gg_home_val,
                gg_away_val,
                over_home_val,
                over_away_val,
                media_gol,  # salviamo media gol nel campo notes
            ),
        )
        imported += 1

    conn.commit()
    conn.close()

    flash(f"✅ {imported} partite importate nella strategia {strategy}.", "success")
    return redirect(url_for("index", strategy=strategy))


@app.route("/clear/<strategy>", methods=["POST"])
@login_required
def clear_strategy(strategy):
    conn = get_db()
    conn.execute("DELETE FROM matches WHERE strategy = ?", (strategy,))
    conn.commit()
    conn.close()
    flash(f"🗑 Dati della strategia {strategy} cancellati.", "success")
    return redirect(url_for("index", strategy=strategy))


@app.route("/export/<strategy>")
@login_required
def export_strategy(strategy):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM matches WHERE strategy = ? ORDER BY match_date, match_time",
        (strategy,)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    if strategy == "GG":
        writer.writerow([
            "Strategia", "Data", "Ora", "Campionato", "Casa", "Trasferta",
            "Mercato", "Quota GG", "ELO GAP", "GG Casa", "GG Trasferta"
        ])
        for m in rows:
            writer.writerow([
                m["strategy"], m["match_date"], m["match_time"], m["championship"],
                m["home_team"], m["away_team"], m["market"], m["odd"],
                m["elo_gap"], m["gg_home"], m["gg_away"]
            ])
    else:
        writer.writerow([
            "Strategia", "Data", "Ora", "Campionato", "Casa", "Trasferta",
            "Mercato", "Quota", "ELO GAP", "Over Casa", "Over Trasferta"
        ])
        for m in rows:
            writer.writerow([
                m["strategy"], m["match_date"], m["match_time"], m["championship"],
                m["home_team"], m["away_team"], m["market"], m["odd"],
                m["elo_gap"], m["over_home"], m["over_away"]
            ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)

    return send_file(
        mem, mimetype="text/csv", as_attachment=True,
        download_name=f"cgmbet_{strategy.replace(' ', '_').replace('.', '')}.csv"
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
