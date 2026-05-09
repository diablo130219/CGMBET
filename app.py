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
        CREATE TABLE IF NOT EXISTS bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capitale REAL DEFAULT 0,
            importo_fisso REAL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
    capitale, importo_fisso = get_bankroll(conn)

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
    capitale, importo_fisso = get_bankroll(conn)

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

    storico = conn.execute(
        "SELECT * FROM bollette ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    capitale, importo_fisso = get_bankroll(conn)

    roi_row = conn.execute(
        "SELECT SUM(profitto) FROM bollette WHERE esito != 'pending'"
    ).fetchone()

    tot_profitto = roi_row[0] or 0
    roi = round((tot_profitto / capitale * 100), 2) if capitale > 0 else 0

    vinte = conn.execute("SELECT COUNT(*) FROM bollette WHERE esito='vinta'").fetchone()[0]
    perse = conn.execute("SELECT COUNT(*) FROM bollette WHERE esito='persa'").fetchone()[0]

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
        roi=roi,
        tot_profitto=tot_profitto,
        vinte=vinte,
        perse=perse,
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
