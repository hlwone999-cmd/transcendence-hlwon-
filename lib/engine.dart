// Kalkulator Lot XAUUSD (IDR) — engine.dart
// Port 1:1 dari script.js versi web. Semua rumus, pembulatan, konstanta,
// dan urutan logika dipertahankan persis sama dengan versi web.

/// Batas lot otomatis: mentok 10.00, lebihnya = peringatan (sama seperti versi web).
const double kMaxLot = 10;

/// Daftar lot tabel: 0.01 … 10.00, kelipatan 0.01 (1000 baris, sama seperti web).
final List<double> kLots =
    List<double>.generate(1000, (int i) => (i + 1) / 100);

/// Persentase kolom matriks (sama seperti konstanta PCTS di web).
const List<int> kPcts = [1, 2, 3, 5, 10, 15, 20, 25, 50, 100];

/// Balance rules n = round(200000 x 1.2^(n-1)) — terverifikasi persis sama
/// dengan PDF tabel (sama seperti konstanta RULE_BALANCES di web).
const List<int> kRuleBalances = [
  200000, 240000, 288000, 345600, 414720, 497664, 597197, 716636, 859963,
  1031956, 1238347, 1486017, 1783220, 2139864, 2567837, 3081404, 3697685,
  4437222, 5324667, 6389600, 7667520, 9201024, 11041229, 13249475, 15899369,
  19079243, 22895092, 27474110, 32968932, 39562719, 47475263, 56970315,
  68364378, 82037254, 98444705,
];

// ---------------------------------------------------------------------------
// Format angka gaya id-ID (sama seperti toLocaleString('id-ID') di web)
// ---------------------------------------------------------------------------

String _group(String digits) {
  final sb = StringBuffer();
  for (int i = 0; i < digits.length; i++) {
    final rem = digits.length - i;
    sb.write(digits[i]);
    if (rem > 1 && rem % 3 == 1) sb.write('.');
  }
  return sb.toString();
}

/// Padanan `idr(x)` web: "Rp" + Math.round + grouping id-ID, "–" bila tak valid.
String idr(num? x) {
  if (x == null || !x.isFinite) return '–';
  return 'Rp${_group(x.round().toString())}';
}

/// Padanan `usd(x)` web: "$" + 2 desimal gaya id-ID (grouping "." desimal ",").
String usd(double? x) {
  if (x == null || !x.isFinite) return '–';
  final s = x.toStringAsFixed(2);
  final parts = s.split('.');
  final negative = parts[0].startsWith('-');
  final digits = negative ? parts[0].substring(1) : parts[0];
  return '${negative ? "-" : ""}\$${_group(digits)},${parts[1]}';
}

/// Padanan `one(x)` web: toLocaleString('id-ID', 1 desimal) → "25,0", "5,5".
String one(num? x) {
  if (x == null || !x.isFinite) return '–';
  final s = x.toDouble().toStringAsFixed(1);
  final parts = s.split('.');
  final negative = parts[0].startsWith('-');
  final digits = negative ? parts[0].substring(1) : parts[0];
  return '${negative ? "-" : ""}${_group(digits)},${parts[1]}';
}

/// Padanan `lotTxt(l)` web: l.toFixed(2) → "0.22" (desimal titik).
String lotTxt(double l) => l.toStringAsFixed(2);

/// Padanan `short(v)` web untuk label sumbu chart: Rp…M / …jt / …rb.
String shortIdr(double v) {
  final a = v.abs();
  if (a >= 1e9) return 'Rp${(v / 1e9).toStringAsFixed(1).replaceFirst('.', ',')}M';
  if (a >= 1e6) return 'Rp${(v / 1e6).toStringAsFixed(1).replaceFirst('.', ',')}jt';
  if (a >= 1e3) return 'Rp${_group((v / 1e3).round().toString())}rb';
  return 'Rp${_group(v.round().toString())}';
}

// ---------------------------------------------------------------------------
// parseAmt — parser angka fleksibel ("10.000.000", "10jt", "1,5jt", dst.)
// ---------------------------------------------------------------------------

final RegExp _cleanRe = RegExp(r'\s|rp|\$|idr|usd');
final RegExp _sufRe = RegExp(r'(jt|juta|rb|ribu|k|m)$');
final RegExp _money3Comma = RegExp(r'^[0-9]{1,3},[0-9]{3}$');
final RegExp _money3Dot = RegExp(r'^[0-9]{1,3}\.[0-9]{3}$');

/// Padanan `parseAmt(raw, isMoney)` web. Mengembalikan null bila NaN.
double? parseAmt(String raw, {bool isMoney = false}) {
  String s = raw.trim().toLowerCase().replaceAll(_cleanRe, '');
  if (s.isEmpty) return null;
  double mult = 1;
  final suf = _sufRe.firstMatch(s);
  if (suf != null) {
    switch (suf.group(1)) {
      case 'jt':
      case 'juta':
      case 'm':
        mult = 1e6;
        break;
      case 'rb':
      case 'ribu':
      case 'k':
        mult = 1e3;
        break;
    }
    s = s.substring(0, s.length - suf.group(1)!.length);
  }
  final dots = '.'.allMatches(s).length;
  final commas = ','.allMatches(s).length;
  if (dots > 0 && commas > 0) {
    // campuran: pemisah terakhir dianggap desimal
    if (s.lastIndexOf(',') > s.lastIndexOf('.')) {
      s = s.replaceAll('.', '').replaceFirst(',', '.');
    } else {
      s = s.replaceAll(',', '');
    }
  } else if (commas > 1) {
    s = s.replaceAll(',', '');
  } else if (dots > 1) {
    s = s.replaceAll('.', '');
  } else if (commas == 1) {
    s = (isMoney && suf == null && _money3Comma.hasMatch(s))
        ? s.replaceAll(',', '')
        : s.replaceFirst(',', '.');
  } else if (dots == 1 && isMoney && suf == null && _money3Dot.hasMatch(s)) {
    s = s.replaceAll('.', '');
  }
  final v = double.tryParse(s);
  if (v == null) return null;
  return v * mult;
}

/// Padanan `Math.round(x*10)/10` web (pembulatan ke 1 desimal, half-up).
double round1dec(double x) => (x * 10).roundToDouble() / 10;

// ---------------------------------------------------------------------------
// Rules Recovery — data & rumus (sama seperti ruleData/rDiv10 di web)
// ---------------------------------------------------------------------------

/// round-half-up eksak (tanpa galat float): round(B x m/10) — pakai integer math.
int rDiv10(int v, int d) => (v * d + 5) ~/ 10;

class RuleData {
  final int n;
  final int b;
  final int after;
  final int sl;
  final int? prev;
  final int next;
  final int diff;
  final double x;
  const RuleData(this.n, this.b, this.after, this.sl, this.prev, this.next,
      this.diff, this.x);
}

RuleData ruleData(int n) {
  final b = kRuleBalances[n - 1];
  final after = rDiv10(b, 9);
  final sl = b - after;
  final prev = n >= 2 ? kRuleBalances[n - 2] : null;
  // rules 1: balik ke balance awal
  final diff = prev != null ? after - prev : b - after;
  final next = rDiv10(b, 12);
  return RuleData(n, b, after, sl, prev, next, diff, diff / after * 100);
}

/// Mode rules: risk 10% · rasio 1:2. Mode recovery: risk X% · rasio 1:1.
const String kRuleNoteNormal =
    'Mode rules: risk 10% dengan rasio 1:2 → menang = +20% naik ke rules berikutnya. Isi "Maksimal SL (pips)" di bawah — lot langsung terhitung.';

// ---------------------------------------------------------------------------
// calc() — tabel utama + pick
// ---------------------------------------------------------------------------

class RowData {
  final double lot, sl, tp;
  final int r, t;
  const RowData(this.lot, this.sl, this.tp, this.r, this.t);
}

class CalcResult {
  final bool ok;
  final double riskIdr, riskUsd, tgtIdr, tgtUsd;
  final List<RowData> rows;
  final int pick; // index baris terpilih, -1 bila tidak ada
  const CalcResult(this.ok, this.riskIdr, this.riskUsd, this.tgtIdr,
      this.tgtUsd, this.rows, this.pick);
}

CalcResult _invalidCalc() => const CalcResult(false, 0, 0, 0, 0, [], -1);

/// Padanan `calc()` web. Input null berarti NaN/invalid dari parseAmt.
CalcResult computeCalc(
    double? bal, double? kurs, double? rp, double? ratio, double? plan) {
  final ok = [bal, kurs, rp, ratio].every((v) => v != null && v.isFinite && v > 0);
  if (!ok) return _invalidCalc();
  final riskIdr = bal! * rp! / 100;
  final riskUsd = riskIdr / kurs!;
  final tgtIdr = riskIdr * ratio!;
  final tgtUsd = tgtIdr / kurs;
  final filled = <RowData>[];
  for (final lot in kLots) {
    final sl = round1dec(riskUsd / (lot * 10));
    final tp = round1dec(sl * ratio);
    filled.add(RowData(lot, sl, tp, (sl * lot * 10 * kurs).round(),
        (tp * lot * 10 * kurs).round()));
  }
  // pick: largest lot whose SL pips still >= planned SL
  int pick = -1;
  if (plan != null && plan.isFinite && plan > 0) {
    for (int i = 0; i < filled.length; i++) {
      if (filled[i].sl >= plan) pick = i;
    }
  }
  return CalcResult(true, riskIdr, riskUsd, tgtIdr, tgtUsd, filled, pick);
}

// ---------------------------------------------------------------------------
// renderAuto() — kotak lot otomatis
// ---------------------------------------------------------------------------

class AutoOut {
  final bool empty; // plan belum diisi
  final String big;
  final bool bad; // teks besar merah (peringatan)
  final String sub, risk, tp, tgt, pip;
  const AutoOut(this.empty, this.big, this.bad, this.sub, this.risk, this.tp,
      this.tgt, this.pip);
}

/// Padanan `renderAuto()` web termasuk cabang peringatan lot > 10.00.
AutoOut computeAuto(double? plan, double bal, double kurs, double rp,
    double ratio, double riskUsd) {
  if (plan == null || !plan.isFinite || plan <= 0) {
    return const AutoOut(true, '–', false,
        'Isi maksimal SL (pips) untuk menghitung lot otomatis.', '–', '–', '–', '–');
  }
  final exact = riskUsd / (plan * 10);
  final lot = (exact * 100 + 1e-9).floorToDouble() / 100;
  final minRiskIdr = plan * 0.01 * 10 * kurs;
  if (lot < 0.01) {
    final needPct = minRiskIdr / bal * 100;
    final needBal = minRiskIdr / (rp / 100);
    final sub = 'Belum cukup untuk 0.01 lot. SL ${one(plan)} pips di 0.01 lot = '
        '${idr(minRiskIdr)}, sedangkan risiko ${one(rp)}% hanya ${idr(bal * rp / 100)}. '
        '${needPct <= 100 ? 'Butuh risk minimal ${needPct.toStringAsFixed(2)}%, atau balance minimal ${idr(needBal)} di risk ${one(rp)}%, atau SL maksimal ${one((riskUsd / 0.1 * 10).floorToDouble() / 10)} pips.' : 'Balance ${idr(bal)} bahkan tidak menutup 0.01 lot untuk SL ini — cek lagi nominal balance.'}';
    return AutoOut(false, '0.00', false, sub, idr(minRiskIdr), one(plan * ratio),
        idr(minRiskIdr * ratio), idr(0.1 * kurs));
  }
  if (lot > kMaxLot) {
    final minSl = ((riskUsd / (kMaxLot * 10) * 10 - 1e-9).ceilToDouble()) / 10;
    return AutoOut(
        false,
        '${lotTxt(kMaxLot)}+',
        true,
        'Butuh ${exact.toStringAsFixed(2)} lot — melebihi batas ${lotTxt(kMaxLot)} lot. '
            'Kecilkan risk % atau perbesar SL minimal ${one(minSl)} pips di ${lotTxt(kMaxLot)} lot.',
        '–',
        '${one(plan * ratio)} pips',
        '–',
        '–');
  }
  final act = plan * lot * 10 * kurs;
  return AutoOut(
      false,
      lot.toStringAsFixed(2),
      false,
      'SL ${one(plan)} pips · lot presisi ${exact.toStringAsFixed(3)} dibulatkan ke bawah',
      '${idr(act)} (${(act / bal * 100).toStringAsFixed(2)}%)',
      '${one(plan * ratio)} pips',
      idr(act * ratio),
      idr(lot * 10 * kurs));
}

// ---------------------------------------------------------------------------
// renderMatrix() — matriks SL pips per lot × % risiko
// ---------------------------------------------------------------------------

class MatrixResult {
  final List<double> pcts;
  final List<List<double>> pips; // [lotIndex][pctIndex]
  final List<int> fitIdx; // per pct, -1 bila tidak ada
  final double rp;
  const MatrixResult(this.pcts, this.pips, this.fitIdx, this.rp);
}

/// Padanan `renderMatrix()` web — mendukung rp desimal seperti web.
MatrixResult? computeMatrix(
    double? bal, double? kurs, double? rp, double? plan) {
  if (bal == null ||
      kurs == null ||
      rp == null ||
      !bal.isFinite ||
      !kurs.isFinite ||
      !rp.isFinite) {
    return null;
  }
  final pcts = (<double>{...kPcts.map((e) => e.toDouble()), rp}).toList()
    ..sort();
  final hasPlan = plan != null && plan.isFinite && plan > 0;
  final n = kLots.length;
  final pips = List.generate(
      n,
      (i) => List<double>.generate(pcts.length, (j) {
            final pc = pcts[j];
            return round1dec(bal * pc / 100 / kurs / (kLots[i] * 10));
          }),
      growable: false);
  final fitIdx = List<int>.generate(pcts.length, (j) {
    if (!hasPlan) return -1;
    int f = -1;
    for (int i = 0; i < n; i++) {
      if (pips[i][j] >= plan) f = i;
    }
    return f;
  });
  return MatrixResult(pcts, pips, fitIdx, rp);
}

// ---------------------------------------------------------------------------
// Util kecil
// ---------------------------------------------------------------------------

/// Padanan `num(id)` web untuk field uang (balance & kurs).
double? numMoney(String raw) => parseAmt(raw, isMoney: true);

/// Padanan `num(id)` web untuk field biasa.
double? numPlain(String raw) => parseAmt(raw);

/// Format integer grouping id-ID tanpa "Rp" (untuk kolom Risk/Target tabel).
String groupInt(int v) => _group(v.toString());

/// Padanan JSON.stringify angka desimal singkat untuk simpanan prefs.
String dec2(double v) => v.toStringAsFixed(2);
