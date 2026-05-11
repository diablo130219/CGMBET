import csv
import io
import os
import json
import sqlite3
from datetime import date, timedelta
from functools import wraps

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

    conn.execute("""
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
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bolletta_oggi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            match_id INTEGER NOT NULL,
            posizione INTEGER DEFAULT 0,
            UNIQUE(data, match_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bollette (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            partite TEXT NOT NULL,
            quota_totale REAL DEFAULT 0,
            importo REAL DEFAULT 0,
            esito TEXT DEFAULT 'pending',
            profitto REAL DEFAULT 0,
            bankroll_pre REAL DEFAULT 0,
            bankroll_post REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS doppie_sessioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            match1_id INTEGER,
            match2_id INTEGER,
            home1 TEXT, away1 TEXT, odd1 REAL DEFAULT 0,
            home2 TEXT, away2 TEXT, odd2 REAL DEFAULT 0,
            quota_doppia REAL DEFAULT 0,
            puntata REAL DEFAULT 0,
            kelly_fraction REAL DEFAULT 0.25,
            esito TEXT DEFAULT 'pending',
            incasso REAL DEFAULT 0,
            profitto REAL DEFAULT 0,
            bankroll_pre REAL DEFAULT 0,
            bankroll_post REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sistema_risultati (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            strategy TEXT NOT NULL,
            match_id INTEGER NOT NULL,
            home_team TEXT,
            away_team TEXT,
            odd REAL DEFAULT 0,
            esito TEXT DEFAULT 'pending',
            flex INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(data, match_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sistema_sessioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            strategy TEXT NOT NULL,
            budget REAL DEFAULT 0,
            n_partite INTEGER DEFAULT 0,
            n_triple INTEGER DEFAULT 0,
            triple_vinte INTEGER DEFAULT 0,
            incasso REAL DEFAULT 0,
            profitto REAL DEFAULT 0,
            roi REAL DEFAULT 0,
            flex_mode INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capitale REAL DEFAULT 0,
            importo_fisso REAL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrazioni leggere per database già esistenti
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(doppie_sessioni)").fetchall()]
    for col, ddl in {
        "market1": "ALTER TABLE doppie_sessioni ADD COLUMN market1 TEXT DEFAULT ''",
        "market2": "ALTER TABLE doppie_sessioni ADD COLUMN market2 TEXT DEFAULT ''",
        "stake_mode": "ALTER TABLE doppie_sessioni ADD COLUMN stake_mode TEXT DEFAULT 'kelly'",
    }.items():
        if col not in existing_cols:
            conn.execute(ddl)

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
    value = str(value).replace(",", ".").replace("%", "").strip()
    try:
        return float(value)
    except ValueError:
        return 0


def normalize_key(k):
    return str(k).strip().lower().replace("{", "").replace("}", "").strip()


def pick(row, names):
    normalized = {normalize_key(k): v for k, v in row.items()}
    for wanted in names:
        wanted_norm = normalize_key(wanted)
        if wanted_norm in normalized:
            return str(normalized[wanted_norm] or "").strip().replace('"', "").strip()

    for wanted in names:
        wanted_norm = normalize_key(wanted)
        for key, value in normalized.items():
            if wanted_norm in key:
                return str(value or "").strip().replace('"', "").strip()
    return ""


def detect_delimiter(text):
    first = text.splitlines()[0] if text.splitlines() else ""
    return ";" if first.count(";") >= first.count(",") else ","


def odd_for_strategy(row, strategy):
    if strategy == "GG":
        return pick(row, ["QUOTA GG", "quota gg", "gg", "quota"])
    if strategy == "Over 2.5":
        return pick(row, ["QUOTA 02.5", "quota o2.5", "quota over 2.5", "over 2.5", "quota"])
    return pick(row, ["QUOTE", "quota over 1.5", "quota o1.5", "over 1.5", "quota"])


def home_stat_for_strategy(row, strategy):
    if strategy == "GG":
        return pick(row, ["GG CASA", "gg casa"])
    if strategy == "Over 2.5":
        return pick(row, ["Over25Casa10", "over25 casa", "over casa"])
    return pick(row, ["over 1.5 casa", "over15 casa", "over casa"])


def away_stat_for_strategy(row, strategy):
    if strategy == "GG":
        return pick(row, ["GG TRASFERTA", "gg trasferta"])
    if strategy == "Over 2.5":
        return pick(row, ["Over25Trasf10", "over25 trasferta", "over trasferta"])
    return pick(row, ["Over 1.5 Trasfe", "over 1.5 trasferta", "over15 trasferta", "over trasferta"])


def media_gol_for_strategy(row, strategy):
    if strategy == "GG":
        return ""
    return pick(row, ["MEDIA GOL", "media gol", "MEDIA GOAL", "media goal"])


def get_counts(conn):
    total_all = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    gg = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy='GG'").fetchone()[0]
    o25 = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy='Over 2.5'").fetchone()[0]
    o15 = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy='Over 1.5'").fetchone()[0]
    return total_all, gg, o25, o15


def get_bankroll(conn):
    bk = conn.execute("SELECT * FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
    capitale = bk["capitale"] if bk else 0
    importo_fisso = bk["importo_fisso"] if bk else 0
    return capitale, importo_fisso




def get_bankroll_stats(conn):
    capitale, importo_fisso = get_bankroll(conn)

    row_profit = conn.execute(
        "SELECT COALESCE(SUM(profitto), 0) FROM bollette WHERE esito != 'pending'"
    ).fetchone()
    tot_profitto = row_profit[0] or 0

    vinte = conn.execute("SELECT COUNT(*) FROM bollette WHERE esito='vinta'").fetchone()[0]
    perse = conn.execute("SELECT COUNT(*) FROM bollette WHERE esito='persa'").fetchone()[0]
    giocate = vinte + perse

    presa_percent = round((vinte / giocate * 100), 2) if giocate > 0 else 0
    capitale_attuale = round(capitale + tot_profitto, 2)
    roi = round((tot_profitto / capitale * 100), 2) if capitale > 0 else 0

    return {
        "capitale": capitale,
        "importo_fisso": importo_fisso,
        "tot_profitto": tot_profitto,
        "capitale_attuale": capitale_attuale,
        "roi": roi,
        "vinte": vinte,
        "perse": perse,
        "giocate": giocate,
        "presa_percent": presa_percent,
    }


def get_bolletta_del_giorno(conn):
    today_str = date.today().isoformat()

    rows_b = conn.execute("""
        SELECT m.*,
        CAST(REPLACE(COALESCE(CASE WHEN m.strategy='GG' THEN m.gg_home ELSE m.over_home END,'0'),',','.') AS REAL) as pct_casa,
        CAST(REPLACE(COALESCE(CASE WHEN m.strategy='GG' THEN m.gg_away ELSE m.over_away END,'0'),',','.') AS REAL) as pct_trasf
        FROM bolletta_oggi bo
        JOIN matches m ON bo.match_id = m.id
        WHERE bo.data = ?
        ORDER BY bo.posizione ASC, bo.id ASC
    """, (today_str,)).fetchall()

    bolletta = []
    quota_totale = 1.0

    for r in rows_b:
        pct_media = ((r["pct_casa"] or 0) + (r["pct_trasf"] or 0)) / 2

        bolletta.append({
            "id": r["id"],
            "home_team": r["home_team"],
            "away_team": r["away_team"],
            "strategy": r["strategy"],
            "market": r["market"],
            "odd": r["odd"],
            "match_date": r["match_date"],
            "match_time": r["match_time"],
            "championship": r["championship"],
            "pct_casa": r["pct_casa"],
            "pct_trasf": r["pct_trasf"],
            "pct_media": round(pct_media, 1),
        })

        if r["odd"] and r["odd"] > 0:
            quota_totale *= r["odd"]

    quota_totale = round(quota_totale, 2) if bolletta else 0
    return bolletta, quota_totale


# ── AUTH ──────────────────────────────────────────────

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


# ── DASHBOARD ─────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    strategy_counts = {"GG": gg_count, "Over 2.5": over25_count, "Over 1.5": over15_count}

    today = date.today()

    today_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date = ?", (today.isoformat(),)
    ).fetchone()[0]

    next3_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=3)).isoformat())
    ).fetchone()[0]

    next7_count = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE match_date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=7)).isoformat())
    ).fetchone()[0]

    avg_odds = {}
    for s in STRATEGIES:
        avg = conn.execute(
            "SELECT AVG(odd) FROM matches WHERE strategy=? AND odd>0", (s,)
        ).fetchone()[0]
        avg_odds[s] = round(avg, 2) if avg else 0

    bolletta, quota_totale = get_bolletta_del_giorno(conn)
    stats = get_bankroll_stats(conn)
    capitale = stats["capitale"]
    importo_fisso = stats["importo_fisso"]

    conn.close()

    return render_template("dashboard.html",
        strategy_counts=strategy_counts,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
        today_count=today_count,
        next3_count=next3_count,
        next7_count=next7_count,
        avg_odds=avg_odds,
        bolletta=bolletta,
        quota_totale=quota_totale,
        bolletta_generata=len(bolletta) > 0,
        capitale=capitale,
        importo_fisso=importo_fisso,
        capitale_attuale=stats["capitale_attuale"],
        tot_profitto=stats["tot_profitto"],
        roi=stats["roi"],
        vinte=stats["vinte"],
        perse=stats["perse"],
        presa_percent=stats["presa_percent"],
        giocate=stats["giocate"],
    )


# ── INDEX PARTITE ─────────────────────────────────────

@app.route("/")
@app.route("/partite")
@login_required
def index():
    strategy = request.args.get("strategy", "GG")
    search = request.args.get("search", "").strip()
    date_filter = request.args.get("date_filter", "")

    query = "SELECT * FROM matches WHERE strategy = ?"
    params = [strategy]

    if search:
        query += " AND (home_team LIKE ? OR away_team LIKE ? OR championship LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like])

    if date_filter == "today":
        query += " AND match_date = ?"
        params.append(date.today().isoformat())
    elif date_filter == "3days":
        query += " AND match_date BETWEEN ? AND ?"
        params.append(date.today().isoformat())
        params.append((date.today() + timedelta(days=3)).isoformat())
    elif date_filter == "7days":
        query += " AND match_date BETWEEN ? AND ?"
        params.append(date.today().isoformat())
        params.append((date.today() + timedelta(days=7)).isoformat())

    query += " ORDER BY match_date ASC, match_time ASC, championship ASC"

    conn = get_db()
    matches = conn.execute(query, params).fetchall()
    total_strategy = conn.execute("SELECT COUNT(*) FROM matches WHERE strategy=?", (strategy,)).fetchone()[0]
    total_all, gg_count, over25_count, over15_count = get_counts(conn)

    bolletta_ids = [
        row["match_id"]
        for row in conn.execute(
            "SELECT match_id FROM bolletta_oggi WHERE data=?",
            (date.today().isoformat(),)
        ).fetchall()
    ]

    bolletta, quota_totale = get_bolletta_del_giorno(conn)
    stats = get_bankroll_stats(conn)
    capitale = stats["capitale"]
    importo_fisso = stats["importo_fisso"]

    conn.close()

    return render_template("index.html",
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
        bolletta_ids=bolletta_ids,
        bolletta=bolletta,
        quota_totale=quota_totale,
        importo_fisso=importo_fisso,
        capitale=capitale,
        capitale_attuale=stats["capitale_attuale"],
        tot_profitto=stats["tot_profitto"],
        roi=stats["roi"],
        vinte=stats["vinte"],
        perse=stats["perse"],
        presa_percent=stats["presa_percent"],
        giocate=stats["giocate"],
    )


# ── IMPORT CSV ────────────────────────────────────────

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

        raw_dt = pick(row, ["Data/Ora", "data/ora", "data", "date"])
        match_date = ""
        match_time = ""

        if raw_dt:
            parts = raw_dt.strip().split()
            raw_d = ""

            if len(parts) >= 3:
                raw_d = parts[1]
                t = parts[2]
                match_time = t[:2] + ":" + t[2:] if len(t) == 4 and t.isdigit() else t
            elif len(parts) == 2:
                raw_d = parts[0]
                match_time = parts[1]
            else:
                raw_d = raw_dt

            try:
                d, m, y = raw_d.strip().split("/")
                match_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            except Exception:
                match_date = raw_d

        odd_val = parse_float(odd_for_strategy(row, strategy))
        home_stat = home_stat_for_strategy(row, strategy)
        away_stat = away_stat_for_strategy(row, strategy)
        media_gol = media_gol_for_strategy(row, strategy)

        gg_home_val = home_stat if strategy == "GG" else ""
        gg_away_val = away_stat if strategy == "GG" else ""
        over_home_val = home_stat if strategy != "GG" else ""
        over_away_val = away_stat if strategy != "GG" else ""

        conn.execute("""
            INSERT INTO matches (
                strategy, match_date, match_time, championship,
                home_team, away_team, market, odd, elo_gap,
                gg_home, gg_away, over_home, over_away, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            strategy,
            match_date,
            match_time,
            pick(row, ["Campionato", "campionato", "league", "lega"]),
            home,
            away,
            strategy,
            odd_val,
            pick(row, ["ELO GAP", "elo gap", "elo"]),
            gg_home_val,
            gg_away_val,
            over_home_val,
            over_away_val,
            media_gol,
        ))

        imported += 1

    conn.commit()
    conn.close()

    flash(f"✅ {imported} partite importate — {strategy}.", "success")
    return redirect(url_for("index", strategy=strategy))


# ── CLEAR ─────────────────────────────────────────────

@app.route("/clear/<strategy>", methods=["POST"])
@login_required
def clear_strategy(strategy):
    conn = get_db()
    conn.execute("DELETE FROM matches WHERE strategy=?", (strategy,))
    conn.commit()
    conn.close()

    flash(f"🗑 Dati di {strategy} cancellati.", "success")
    return redirect(url_for("index", strategy=strategy))


# ── EXPORT ────────────────────────────────────────────

@app.route("/export/<strategy>")
@login_required
def export_strategy(strategy):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM matches WHERE strategy=? ORDER BY match_date, match_time", (strategy,)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    if strategy == "GG":
        writer.writerow(["Strategia", "Data", "Ora", "Campionato", "Casa", "Trasferta", "Mercato", "Quota GG", "ELO GAP", "GG Casa", "GG Trasferta", "Media Gol"])
        for m in rows:
            writer.writerow([m["strategy"], m["match_date"], m["match_time"], m["championship"], m["home_team"], m["away_team"], m["market"], m["odd"], m["elo_gap"], m["gg_home"], m["gg_away"], m["notes"]])
    else:
        writer.writerow(["Strategia", "Data", "Ora", "Campionato", "Casa", "Trasferta", "Mercato", "Quota", "ELO GAP", "Over Casa", "Over Trasferta", "Media Gol"])
        for m in rows:
            writer.writerow([m["strategy"], m["match_date"], m["match_time"], m["championship"], m["home_team"], m["away_team"], m["market"], m["odd"], m["elo_gap"], m["over_home"], m["over_away"], m["notes"]])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)

    return send_file(mem, mimetype="text/csv", as_attachment=True,
                     download_name=f"cgmbet_{strategy.replace(' ', '_').replace('.', '')}.csv")


# ── BOLLETTA MANUALE ──────────────────────────────────

@app.route("/aggiungi-bolletta/<int:match_id>", methods=["POST"])
@login_required
def aggiungi_bolletta(match_id):
    today_str = date.today().isoformat()
    next_url = request.form.get("next") or request.referrer or url_for("index")

    conn = get_db()

    exists = conn.execute(
        "SELECT id FROM bolletta_oggi WHERE data=? AND match_id=?",
        (today_str, match_id)
    ).fetchone()

    if exists:
        conn.close()
        flash("ℹ️ Partita già presente nella Bolletta Pazza.", "success")
        return redirect(next_url)

    max_pos = conn.execute(
        "SELECT COALESCE(MAX(posizione), 0) FROM bolletta_oggi WHERE data=?",
        (today_str,)
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO bolletta_oggi (data, match_id, posizione) VALUES (?, ?, ?)",
        (today_str, match_id, max_pos + 1)
    )

    conn.commit()
    conn.close()

    flash("✅ Partita aggiunta alla Bolletta Pazza.", "success")
    return redirect(next_url)


@app.route("/rimuovi-da-bolletta/<int:match_id>", methods=["POST"])
@login_required
def rimuovi_da_bolletta(match_id):
    today_str = date.today().isoformat()
    next_url = request.form.get("next") or request.referrer or url_for("bolletta_page")

    conn = get_db()
    conn.execute(
        "DELETE FROM bolletta_oggi WHERE data=? AND match_id=?",
        (today_str, match_id)
    )
    conn.commit()
    conn.close()

    flash("✕ Partita rimossa dalla Bolletta Pazza.", "success")
    return redirect(next_url)


@app.route("/svuota-bolletta", methods=["POST"])
@login_required
def svuota_bolletta():
    today_str = date.today().isoformat()

    conn = get_db()
    conn.execute("DELETE FROM bolletta_oggi WHERE data=?", (today_str,))
    conn.commit()
    conn.close()

    flash("🧹 Bolletta Pazza svuotata.", "success")
    return redirect(request.referrer or url_for("bolletta_page"))


@app.route("/genera-bolletta", methods=["POST"])
@login_required
def genera_bolletta():
    today_str = date.today().isoformat()

    conn = get_db()
    conn.execute("DELETE FROM bolletta_oggi WHERE data=?", (today_str,))

    rows = conn.execute("""
        SELECT id,
        CAST(REPLACE(COALESCE(CASE WHEN strategy='GG' THEN gg_home ELSE over_home END,'0'),',','.') AS REAL) as pct_casa,
        CAST(REPLACE(COALESCE(CASE WHEN strategy='GG' THEN gg_away ELSE over_away END,'0'),',','.') AS REAL) as pct_trasf
        FROM matches
        WHERE match_date=?
        ORDER BY (
            CAST(REPLACE(COALESCE(CASE WHEN strategy='GG' THEN gg_home ELSE over_home END,'0'),',','.') AS REAL) +
            CAST(REPLACE(COALESCE(CASE WHEN strategy='GG' THEN gg_away ELSE over_away END,'0'),',','.') AS REAL)
        ) DESC
        LIMIT 8
    """, (today_str,)).fetchall()

    for idx, row in enumerate(rows, start=1):
        conn.execute(
            "INSERT OR IGNORE INTO bolletta_oggi (data, match_id, posizione) VALUES (?, ?, ?)",
            (today_str, row["id"], idx)
        )

    conn.commit()
    conn.close()

    flash(f"🎰 Bolletta Pazza generata con {len(rows)} partite.", "success")
    return redirect(url_for("bolletta_page"))


@app.route("/bolletta")
@login_required
def bolletta_page():
    conn = get_db()

    bolletta, quota_totale = get_bolletta_del_giorno(conn)

    storico_rows = conn.execute(
        "SELECT * FROM bollette ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    storico = []
    for b in storico_rows:
        try:
            dettagli = json.loads(b["partite"] or "[]")
        except Exception:
            dettagli = []

        storico.append({
            "id": b["id"],
            "data": b["data"],
            "partite": b["partite"],
            "quota_totale": b["quota_totale"],
            "importo": b["importo"],
            "esito": b["esito"],
            "profitto": b["profitto"],
            "bankroll_pre": b["bankroll_pre"],
            "bankroll_post": b["bankroll_post"],
            "created_at": b["created_at"],
            "dettagli": dettagli,
        })

    stats = get_bankroll_stats(conn)
    capitale = stats["capitale"]
    importo_fisso = stats["importo_fisso"]
    capitale_attuale = stats["capitale_attuale"]
    tot_profitto = stats["tot_profitto"]
    roi = stats["roi"]
    vinte = stats["vinte"]
    perse = stats["perse"]
    presa_percent = stats["presa_percent"]
    giocate = stats["giocate"]

    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    conn.close()

    partite_json = json.dumps([{
        "home": p["home_team"],
        "away": p["away_team"],
        "mercato": p["market"],
        "quota": p["odd"]
    } for p in bolletta])

    return render_template("bolletta.html",
        bolletta=bolletta,
        quota_totale=quota_totale,
        bolletta_generata=len(bolletta) > 0,
        oggi=date.today().strftime("%d/%m/%Y"),
        storico=storico,
        capitale=capitale,
        importo_fisso=importo_fisso,
        capitale_attuale=capitale_attuale,
        roi=roi,
        tot_profitto=tot_profitto,
        vinte=vinte,
        perse=perse,
        presa_percent=presa_percent,
        giocate=giocate,
        partite_json=partite_json,
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
    )


@app.route("/update-quota/<int:match_id>", methods=["POST"])
@login_required
def update_quota(match_id):
    quota = parse_float(request.form.get("quota"))
    source = request.form.get("source", "")

    conn = get_db()
    conn.execute("UPDATE matches SET odd=? WHERE id=?", (quota, match_id))
    conn.commit()
    conn.close()

    flash("✅ Quota aggiornata.", "success")

    if source == "bolletta":
        return redirect(url_for("bolletta_page"))

    return redirect(request.referrer or url_for("index"))



@app.route("/aggiorna-importo-bolletta", methods=["POST"])
@login_required
def aggiorna_importo_bolletta():
    importo_fisso = parse_float(request.form.get("importo_fisso"))
    capitale_form = parse_float(request.form.get("capitale"))

    conn = get_db()
    capitale, _ = get_bankroll(conn)
    if capitale_form > 0:
        capitale = capitale_form

    conn.execute(
        "INSERT INTO bankroll (capitale, importo_fisso) VALUES (?, ?)",
        (capitale, importo_fisso)
    )
    conn.commit()
    conn.close()

    flash("💶 Importo giocato aggiornato.", "success")
    return redirect(request.referrer or url_for("bolletta_page"))


@app.route("/salva-bankroll", methods=["POST"])
@login_required
def salva_bankroll():
    capitale = parse_float(request.form.get("capitale"))
    importo_fisso = parse_float(request.form.get("importo_fisso"))

    conn = get_db()
    conn.execute(
        "INSERT INTO bankroll (capitale, importo_fisso) VALUES (?, ?)",
        (capitale, importo_fisso)
    )
    conn.commit()
    conn.close()

    flash("💾 Bankroll salvato.", "success")
    return redirect(request.referrer or url_for("bolletta_page"))


@app.route("/salva-bolletta", methods=["POST"])
@login_required
def salva_bolletta():
    data = date.today().isoformat()
    quota_totale = parse_float(request.form.get("quota_totale"))
    importo = parse_float(request.form.get("importo"))
    bankroll_pre = parse_float(request.form.get("bankroll_pre"))
    partite_json = request.form.get("partite_json", "[]")

    conn = get_db()
    conn.execute("""
        INSERT INTO bollette (data, partite, quota_totale, importo, esito, profitto, bankroll_pre, bankroll_post)
        VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
    """, (data, partite_json, quota_totale, importo, bankroll_pre, bankroll_pre))
    conn.commit()
    conn.close()

    flash("💾 Bolletta salvata nello storico.", "success")
    return redirect(url_for("bolletta_page"))



@app.route("/elimina-bolletta/<int:bolletta_id>", methods=["POST"])
@login_required
def elimina_bolletta_salvata(bolletta_id):
    conn = get_db()
    conn.execute("DELETE FROM bollette WHERE id = ?", (bolletta_id,))
    conn.commit()
    conn.close()

    flash("🗑 Bolletta salvata eliminata.", "success")
    return redirect(url_for("bolletta_page"))


@app.route("/esito-bolletta/<int:bolletta_id>", methods=["POST"])
@login_required
def esito_bolletta(bolletta_id):
    esito = request.form.get("esito")
    if esito not in ["vinta", "persa", "pending"]:
        esito = "pending"

    conn = get_db()
    b = conn.execute("SELECT * FROM bollette WHERE id=?", (bolletta_id,)).fetchone()

    if not b:
        conn.close()
        flash("Bolletta non trovata.", "error")
        return redirect(url_for("bolletta_page"))

    profitto = 0
    bankroll_post = b["bankroll_pre"]

    if esito == "vinta":
        profitto = (b["importo"] * b["quota_totale"]) - b["importo"]
        bankroll_post = b["bankroll_pre"] + profitto
    elif esito == "persa":
        profitto = -b["importo"]
        bankroll_post = b["bankroll_pre"] + profitto

    conn.execute(
        "UPDATE bollette SET esito=?, profitto=?, bankroll_post=? WHERE id=?",
        (esito, profitto, bankroll_post, bolletta_id)
    )
    conn.commit()
    conn.close()

    flash("✅ Esito aggiornato.", "success")
    return redirect(url_for("bolletta_page"))



# ══════════════════════════════════════════════════════════════════════════════
# SISTEMA TRIPLE
# ══════════════════════════════════════════════════════════════════════════════

SISTEMI_RIDUZIONE = {
    4: [(0,1,2),(0,1,3),(0,2,3),(1,2,3)],
    5: [(0,1,2),(0,1,3),(0,2,3),(1,2,3)],
    6: [(0,1,2),(0,3,4),(1,3,5),(2,3,5),(0,2,5),(1,2,4)],
    7: [(0,1,2),(0,3,4),(0,5,6),(1,3,5),(1,4,6),(2,3,6),(2,4,5)],
}

GARANZIE = {
    4: "Sbagli 1 su 4 - 3 triple vinte",
    5: "Sbagli 2 su 5 - 1 tripla vinta",
    6: "Sbagli 2 su 6 - 1 tripla vinta",
    7: "Sbagli 3 su 7 - 1 tripla vinta",
}

SOGLIE = {
    "Over 1.5": {"casa": 80, "trasf": 75, "quota_min": 1.40, "quota_max": 1.80, "media_gol": None, "max_filtri": 3},
    "Over 2.5": {"casa": 75, "trasf": 70, "quota_min": 1.70, "quota_max": 2.10, "media_gol": 3.00, "max_filtri": 4},
    "GG":       {"casa": 70, "trasf": 70, "quota_min": 1.60, "quota_max": 2.10, "media_gol": None, "max_filtri": 3},
}


def calcola_score(m, strategia):
    s = SOGLIE[strategia]
    if strategia == "GG":
        val_h = parse_float(m["gg_home"])
        val_a = parse_float(m["gg_away"])
    else:
        val_h = parse_float(m["over_home"])
        val_a = parse_float(m["over_away"])

    ok_h = val_h >= s["casa"]
    ok_a = val_a >= s["trasf"]
    ok_q = s["quota_min"] <= float(m["odd"] or 0) <= s["quota_max"]
    score = int(ok_h) + int(ok_a) + int(ok_q)

    dettagli = {
        "casa":  {"val": val_h, "ok": ok_h, "soglia": s["casa"]},
        "trasf": {"val": val_a, "ok": ok_a, "soglia": s["trasf"]},
        "quota": {"val": float(m["odd"] or 0), "ok": ok_q,
                  "soglia": str(s["quota_min"]) + "-" + str(s["quota_max"])},
    }

    if s["media_gol"] is not None:
        media = parse_float(m["notes"])
        ok_mg = media >= s["media_gol"]
        dettagli["media_gol"] = {"val": media, "ok": ok_mg, "soglia": s["media_gol"]}
        score += int(ok_mg)

    dettagli["score"] = score
    dettagli["max"]   = s["max_filtri"]
    dettagli["semaforo"] = (
        "entra"      if score == s["max_filtri"] else
        "borderline" if score == s["max_filtri"] - 1 else
        "scarta"
    )
    return dettagli


def calcola_sistema(partite, budget):
    n = min(len(partite), 7)
    if n < 4:
        return None
    partite = partite[:n]
    combos  = SISTEMI_RIDUZIONE[n]

    triple = []
    for combo in combos:
        q    = 1.0
        nomi = []
        for idx in combo:
            q *= float(partite[idx]["odd"] or 1)
            nomi.append(partite[idx]["home_team"] + " - " + partite[idx]["away_team"])
        triple.append({"partite": nomi, "quota": round(q, 2)})

    inv_sum = sum(1.0 / t["quota"] for t in triple if t["quota"] > 0)
    for t in triple:
        t["puntata"] = round(budget * (1.0 / t["quota"]) / inv_sum, 2) if inv_sum > 0 else 0
        t["vincita"] = round(t["puntata"] * t["quota"], 2)

    vincita_min = min(t["vincita"] for t in triple)
    vincita_max = sum(t["vincita"] for t in triple)

    return {
        "n_partite": n,
        "n_triple":  len(combos),
        "triple":    triple,
        "budget":    budget,
        "vincita_min":  round(vincita_min, 2),
        "vincita_max":  round(vincita_max, 2),
        "profitto_min": round(vincita_min - budget, 2),
        "profitto_max": round(vincita_max - budget, 2),
        "roi_min": round((vincita_min - budget) / budget * 100, 1) if budget > 0 else 0,
        "garanzia": GARANZIE.get(n, ""),
    }


@app.route("/sistema")
@login_required
def sistema():
    strategy = request.args.get("strategy", "Over 2.5")
    budget   = parse_float(request.args.get("budget", "10"))
    today    = date.today()

    conn = get_db()
    matches = conn.execute(
        "SELECT * FROM matches WHERE strategy=? AND match_date=? ORDER BY odd DESC",
        (strategy, today.isoformat())
    ).fetchall()

    # Carica risultati di oggi
    risultati_oggi = {}
    for r in conn.execute(
        "SELECT match_id, esito FROM sistema_risultati WHERE data=? AND strategy=?",
        (today.isoformat(), strategy)
    ).fetchall():
        risultati_oggi[r["match_id"]] = r["esito"]

    # Storico sessioni
    storico_sessioni = conn.execute(
        "SELECT * FROM sistema_sessioni WHERE strategy=? ORDER BY created_at DESC LIMIT 10",
        (strategy,)
    ).fetchall()

    # Stats totali sistema
    stats_sistema = conn.execute("""
        SELECT
            COUNT(*) as totale_sessioni,
            COALESCE(SUM(profitto),0) as tot_profitto,
            COALESCE(SUM(budget),0) as tot_investito,
            COALESCE(SUM(triple_vinte),0) as tot_triple_vinte,
            COALESCE(SUM(n_triple),0) as tot_triple_giocate
        FROM sistema_sessioni WHERE strategy=?
    """, (strategy,)).fetchone()

    partite_scored = []
    for m in matches:
        det = calcola_score(m, strategy)
        partite_scored.append({
            "id":           m["id"],
            "home_team":    m["home_team"],
            "away_team":    m["away_team"],
            "championship": m["championship"],
            "match_time":   m["match_time"],
            "odd":          m["odd"],
            "gg_home":      m["gg_home"],
            "gg_away":      m["gg_away"],
            "over_home":    m["over_home"],
            "over_away":    m["over_away"],
            "notes":        m["notes"],
            "filtri":       det,
        })

    ordine = {"entra": 0, "borderline": 1, "scarta": 2}
    partite_scored.sort(key=lambda x: ordine[x["filtri"]["semaforo"]])

    entra = [p for p in partite_scored if p["filtri"]["semaforo"] == "entra"]
    borderline = [p for p in partite_scored if p["filtri"]["semaforo"] == "borderline"]

    # Modalità FLEX — se ENTRA < 4, completa con le migliori BORDERLINE
    flex_mode = False
    partite_sistema = entra[:]
    if len(partite_sistema) < 4 and borderline:
        # Ordina borderline per score decrescente
        borderline_sorted = sorted(borderline, key=lambda x: x["filtri"]["score"], reverse=True)
        needed = 4 - len(partite_sistema)
        flex_aggiunte = borderline_sorted[:needed]
        for p in flex_aggiunte:
            p["flex"] = True  # marca come flex
        partite_sistema += flex_aggiunte
        flex_mode = len(flex_aggiunte) > 0

    sistema_result = calcola_sistema(partite_sistema, budget) if len(partite_sistema) >= 4 else None
    if sistema_result:
        sistema_result["flex_mode"] = flex_mode
        sistema_result["n_entra"] = len(entra)
        sistema_result["n_flex"] = len(partite_sistema) - len(entra)

    confronto = {}
    for s in STRATEGIES:
        ms = conn.execute(
            "SELECT * FROM matches WHERE strategy=? AND match_date=?",
            (s, today.isoformat())
        ).fetchall()
        confronto[s] = sum(1 for m in ms if calcola_score(m, s)["semaforo"] == "entra")

    migliore = max(confronto, key=confronto.get) if any(confronto.values()) else None

    stats = get_bankroll_stats(conn)
    bolletta, quota_totale = get_bolletta_del_giorno(conn)
    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    conn.close()

    return render_template("sistema.html",
        strategy=strategy,
        risultati_oggi=risultati_oggi,
        storico_sessioni=storico_sessioni,
        stats_sistema=stats_sistema,
        budget=budget,
        partite=partite_scored,
        entra=entra,
        borderline=borderline,
        partite_sistema=partite_sistema,
        flex_mode=flex_mode,
        sistema=sistema_result,
        soglie=SOGLIE[strategy],
        confronto=confronto,
        migliore=migliore,
        oggi=today.strftime("%d/%m/%Y"),
        bolletta=bolletta,
        quota_totale=quota_totale,
        importo_fisso=stats["importo_fisso"],
        capitale=stats["capitale"],
        capitale_attuale=stats["capitale_attuale"],
        tot_profitto=stats["tot_profitto"],
        roi=stats["roi"],
        vinte=stats["vinte"],
        perse=stats["perse"],
        presa_percent=stats["presa_percent"],
        giocate=stats["giocate"],
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
    )


@app.route("/sistema/esito/<int:match_id>", methods=["POST"])
@login_required
def sistema_esito(match_id):
    esito = request.form.get("esito", "pending")
    if esito not in ["vinta", "persa", "pending"]:
        esito = "pending"

    data = date.today().isoformat()
    strategy = request.form.get("strategy", "Over 2.5")
    budget = parse_float(request.form.get("budget", "10"))

    conn = get_db()
    try:
        # Recupera dati partita in modo sicuro
        m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            flash("⚠️ Partita non trovata: ricarica il CSV o torna alla pagina sistemi.", "error")
            return redirect(url_for("sistema", strategy=strategy, budget=budget))

        # Usa una logica update-first: evita errori di pagina su vincoli UNIQUE
        updated = conn.execute("""
            UPDATE sistema_risultati
            SET esito=?, strategy=?, home_team=?, away_team=?, odd=?
            WHERE data=? AND match_id=?
        """, (esito, strategy, m["home_team"], m["away_team"], m["odd"], data, match_id)).rowcount

        if updated == 0:
            try:
                conn.execute("""
                    INSERT INTO sistema_risultati
                    (data, strategy, match_id, home_team, away_team, odd, esito, flex)
                    VALUES (?,?,?,?,?,?,?,0)
                """, (data, strategy, match_id, m["home_team"], m["away_team"], m["odd"], esito))
            except Exception:
                # Se il database vecchio ha un vincolo diverso, riprova aggiornando per match_id.
                conn.execute("""
                    UPDATE sistema_risultati
                    SET esito=?, strategy=?, home_team=?, away_team=?, odd=?
                    WHERE match_id=?
                """, (esito, strategy, m["home_team"], m["away_team"], m["odd"], match_id))

        conn.commit()
        flash("✅ Esito sistema aggiornato.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"⚠️ Non sono riuscito ad aggiornare l'esito: {e}", "error")
    finally:
        conn.close()

    return redirect(url_for("sistema", strategy=strategy, budget=budget))


@app.route("/sistema/salva-sessione", methods=["POST"])
@login_required
def sistema_salva_sessione():
    data = date.today().isoformat()
    strategy = request.form.get("strategy", "Over 2.5")
    budget = parse_float(request.form.get("budget", "10"))
    n_partite = int(request.form.get("n_partite", 0))
    n_triple = int(request.form.get("n_triple", 0))
    triple_vinte = int(request.form.get("triple_vinte", 0))
    incasso = parse_float(request.form.get("incasso", "0"))
    profitto = parse_float(request.form.get("profitto", "0"))
    roi = parse_float(request.form.get("roi", "0"))
    flex_mode = int(request.form.get("flex_mode", "0"))

    conn = get_db()
    conn.execute("""
        INSERT INTO sistema_sessioni
        (data, strategy, budget, n_partite, n_triple, triple_vinte, incasso, profitto, roi, flex_mode)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (data, strategy, budget, n_partite, n_triple, triple_vinte, incasso, profitto, roi, flex_mode))
    conn.commit()
    conn.close()

    flash("💾 Sessione sistema salvata.", "success")
    return redirect(url_for("sistema", strategy=strategy, budget=budget))



# ══════════════════════════════════════════════════════════════════════════════
# SISTEMA DOPPIE — Kelly 25%
# ══════════════════════════════════════════════════════════════════════════════

def calcola_kelly(prob_stimata, quota, bankroll, fraction=0.25):
    """Kelly frazionato. prob_stimata = % storica / 100"""
    if quota <= 1 or prob_stimata <= 0:
        return 0
    b = quota - 1  # profitto netto per unità
    q = 1 - prob_stimata
    kelly = (b * prob_stimata - q) / b
    kelly = max(0, kelly)  # no Kelly negativo
    puntata = bankroll * kelly * fraction
    return round(puntata, 2)


def get_doppie_oggi(conn):
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM doppie_sessioni WHERE data=? ORDER BY id ASC",
        (today,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_bankroll_doppie(conn):
    """Bankroll attuale = capitale iniziale + somma profitti doppie"""
    capitale, _ = get_bankroll(conn)
    profitti = conn.execute(
        "SELECT COALESCE(SUM(profitto),0) FROM doppie_sessioni WHERE esito != 'pending'"
    ).fetchone()[0]
    return round(capitale + profitti, 2)


@app.route("/doppie")
@login_required
def doppie_page():
    strategy = request.args.get("strategy", "GG")
    today = date.today()

    conn = get_db()

    # Partite di oggi del mercato attivo + tutte le partite per creare doppie miste
    matches = conn.execute(
        "SELECT * FROM matches WHERE strategy=? AND match_date=? ORDER BY odd ASC",
        (strategy, today.isoformat())
    ).fetchall()
    all_matches = conn.execute(
        "SELECT * FROM matches WHERE match_date=? ORDER BY strategy ASC, match_time ASC, odd ASC",
        (today.isoformat(),)
    ).fetchall()

    # Bankroll attuale
    bankroll = get_bankroll_doppie(conn)
    capitale, importo_fisso = get_bankroll(conn)

    # Doppie di oggi
    doppie_oggi = get_doppie_oggi(conn)

    # Stats doppie
    stats_doppie = conn.execute("""
        SELECT
            COUNT(*) as totale,
            COALESCE(SUM(CASE WHEN esito='vinta' THEN 1 ELSE 0 END),0) as vinte,
            COALESCE(SUM(CASE WHEN esito='persa' THEN 1 ELSE 0 END),0) as perse,
            COALESCE(SUM(profitto),0) as tot_profitto,
            COALESCE(SUM(puntata),0) as tot_investito
        FROM doppie_sessioni WHERE esito != 'pending'
    """).fetchone()

    roi_doppie = 0
    if stats_doppie["tot_investito"] > 0:
        roi_doppie = round(stats_doppie["tot_profitto"] / stats_doppie["tot_investito"] * 100, 1)

    # Storico ultime 20
    storico = conn.execute(
        "SELECT * FROM doppie_sessioni ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    total_all, gg_count, over25_count, over15_count = get_counts(conn)
    conn.close()

    return render_template("doppie.html",
        strategy=strategy,
        matches=matches,
        all_matches=all_matches,
        doppie_oggi=doppie_oggi,
        bankroll=bankroll,
        capitale=capitale,
        importo_fisso=importo_fisso,
        stats_doppie=stats_doppie,
        roi_doppie=roi_doppie,
        storico=storico,
        oggi=today.strftime("%d/%m/%Y"),
        total_all=total_all,
        gg_count=gg_count,
        over25_count=over25_count,
        over15_count=over15_count,
    )


@app.route("/doppie/aggiungi", methods=["POST"])
@login_required
def doppie_aggiungi():
    strategy = request.form.get("strategy", "GG")
    match1_id = request.form.get("match1_id")
    match2_id = request.form.get("match2_id")
    stake_mode = request.form.get("stake_mode", "kelly")
    puntata_manual = parse_float(request.form.get("puntata_manual"))
    kelly_fraction = parse_float(request.form.get("kelly_fraction", "25")) / 100
    if kelly_fraction <= 0:
        kelly_fraction = 0.25
    today = date.today().isoformat()

    if not match1_id or not match2_id or match1_id == match2_id:
        flash("⚠️ Seleziona 2 partite diverse.", "error")
        return redirect(url_for("doppie_page", strategy=strategy))

    conn = get_db()
    m1 = conn.execute("SELECT * FROM matches WHERE id=?", (match1_id,)).fetchone()
    m2 = conn.execute("SELECT * FROM matches WHERE id=?", (match2_id,)).fetchone()

    if not m1 or not m2:
        flash("⚠️ Partite non trovate.", "error")
        conn.close()
        return redirect(url_for("doppie_page", strategy=strategy))

    quota_doppia = round(float(m1["odd"] or 1) * float(m2["odd"] or 1), 2)

    # Bankroll e stake: manuale oppure Kelly frazionato
    bankroll = get_bankroll_doppie(conn)

    def prob_match(m):
        if m["strategy"] == "GG":
            return ((parse_float(m["gg_home"]) + parse_float(m["gg_away"])) / 2) / 100
        return ((parse_float(m["over_home"]) + parse_float(m["over_away"])) / 2) / 100

    prob_doppia = prob_match(m1) * prob_match(m2)
    if stake_mode == "manuale" and puntata_manual > 0:
        puntata = puntata_manual
    else:
        puntata = calcola_kelly(prob_doppia, quota_doppia, bankroll, fraction=kelly_fraction)
        stake_mode = "kelly"

    # Protezione stake: il Kelly non deve mai “sparare” puntate troppo alte.
    # Regola pratica: minimo €1, massimo 2x lo stake fisso preferito.
    # Se lo stake fisso non è impostato, usa un tetto prudente di €4.
    _, importo_fisso = get_bankroll(conn)
    max_stake = round((importo_fisso * 2), 2) if importo_fisso and importo_fisso > 0 else 4.0
    if bankroll > 0:
        # ulteriore sicurezza: mai oltre il 4% del bankroll
        max_stake = min(max_stake, round(bankroll * 0.04, 2))

    puntata = max(1.0, min(puntata, max_stake))
    puntata = round(puntata, 2)

    conn.execute("""
        INSERT INTO doppie_sessioni
        (data, match1_id, match2_id, home1, away1, odd1, market1, home2, away2, odd2, market2,
         quota_doppia, puntata, kelly_fraction, bankroll_pre, stake_mode)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (today, match1_id, match2_id,
          m1["home_team"], m1["away_team"], m1["odd"], m1["strategy"],
          m2["home_team"], m2["away_team"], m2["odd"], m2["strategy"],
          quota_doppia, puntata, kelly_fraction, bankroll, stake_mode))
    conn.commit()
    conn.close()

    flash(f"✅ Doppia aggiunta — puntata: €{puntata}", "success")
    return redirect(url_for("doppie_page", strategy=strategy))


@app.route("/doppie/esito/<int:doppia_id>", methods=["POST"])
@login_required
def doppie_esito(doppia_id):
    esito = request.form.get("esito", "pending")
    strategy = request.form.get("strategy", "GG")

    conn = get_db()
    d = conn.execute("SELECT * FROM doppie_sessioni WHERE id=?", (doppia_id,)).fetchone()

    if d:
        incasso = 0
        profitto = 0
        bankroll_post = d["bankroll_pre"]

        if esito == "vinta":
            incasso = round(d["puntata"] * d["quota_doppia"], 2)
            profitto = round(incasso - d["puntata"], 2)
            bankroll_post = round(d["bankroll_pre"] + profitto, 2)
        elif esito == "persa":
            profitto = -d["puntata"]
            bankroll_post = round(d["bankroll_pre"] - d["puntata"], 2)

        conn.execute("""
            UPDATE doppie_sessioni
            SET esito=?, incasso=?, profitto=?, bankroll_post=?
            WHERE id=?
        """, (esito, incasso, profitto, bankroll_post, doppia_id))
        conn.commit()

    conn.close()
    flash("✅ Esito aggiornato.", "success")
    return redirect(url_for("doppie_page", strategy=strategy))


@app.route("/doppie/elimina/<int:doppia_id>", methods=["POST"])
@login_required
def doppie_elimina(doppia_id):
    strategy = request.form.get("strategy", "GG")
    conn = get_db()
    conn.execute("DELETE FROM doppie_sessioni WHERE id=?", (doppia_id,))
    conn.commit()
    conn.close()
    flash("🗑 Doppia eliminata.", "success")
    return redirect(url_for("doppie_page", strategy=strategy))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
