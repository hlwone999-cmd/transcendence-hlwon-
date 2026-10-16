// Kalkulator Lot XAUUSD (IDR) — montecarlo.dart
// Port 1:1 dari runMC() di script.js versi web.
// Murni fungsi (tanpa Flutter) supaya bisa dijalankan di Isolate.
import 'dart:math' as math;
import 'dart:typed_data';

class McParams {
  final double bal, rp, rr, sl, wr, cost, dd;
  final int nTrades, nSim;
  final bool comp;
  const McParams(this.bal, this.rp, this.rr, this.sl, this.wr, this.cost,
      this.dd, this.nTrades, this.nSim, this.comp);
}

class McResult {
  final int n, s, hitDD, blown;
  final double bal, dd, cR, exp, beWr, pProfit;
  /// bands[t] = [p05, p25, p50, p75, p95] pada trade ke-t (0..N)
  final List<List<double>> bands;
  final List<double> ends, mdd, streak; // sudah terurut
  const McResult(this.n, this.s, this.hitDD, this.blown, this.bal, this.dd,
      this.cR, this.exp, this.beWr, this.pProfit, this.bands, this.ends,
      this.mdd, this.streak);
}

/// Padanan `pctl(a,p)` web (a sudah terurut).
double pctl(List<double> a, double p) {
  if (a.isEmpty) return double.nan;
  final i = (a.length - 1) * p;
  final lo = i.floor();
  final hi = i.ceil();
  return a[lo] + (a[hi] - a[lo]) * (i - lo);
}

/// Padanan `runMC()` web. `rnd` boleh null (pakai Random baru).
McResult runMc(McParams p, {math.Random? rnd}) {
  final r = rnd ?? math.Random();
  final bal = p.bal, rp = p.rp, rr = p.rr, sl = p.sl, wr = p.wr;
  final N = p.nTrades, S = p.nSim;
  final cR = (sl.isFinite && sl > 0) ? p.cost / sl : 0.0; // biaya dalam satuan R
  final win = rr - cR, loss = 1 + cR; // hasil per trade dalam R
  final exp = wr * win - (1 - wr) * loss;
  final beWr = loss / (win + loss);

  final cols = List<Float64List>.generate(N + 1, (_) => Float64List(S));
  final ends = Float64List(S), mdd = Float64List(S), streak = Int32List(S);
  int hitDD = 0, blown = 0;
  for (int k = 0; k < S; k++) {
    double b = bal, peak = bal, worst = 0;
    int run = 0, best = 0;
    bool hit = false;
    cols[0][k] = b;
    for (int t = 1; t <= N; t++) {
      final riskAmt = p.comp ? b * rp : bal * rp;
      if (b > 0) {
        if (r.nextDouble() < wr) {
          b += riskAmt * win;
          run = 0;
        } else {
          b -= riskAmt * loss;
          run++;
          if (run > best) best = run;
        }
      }
      if (b <= 0) b = 0;
      if (b > peak) peak = b;
      final d = (peak - b) / peak;
      if (d > worst) worst = d;
      if (d >= p.dd) hit = true;
      cols[t][k] = b;
    }
    ends[k] = b;
    mdd[k] = worst;
    streak[k] = best;
    if (hit) hitDD++;
    if (b <= bal * 0.01) blown++;
  }
  final bands = List<List<double>>.generate(N + 1, (t) {
    final a = cols[t].toList()..sort();
    return [.05, .25, .5, .75, .95].map((q) => pctl(a, q)).toList();
  });
  final e = ends.toList()..sort();
  final m = mdd.toList()..sort();
  final st = streak.map((e) => e.toDouble()).toList()..sort();
  int profitCount = 0;
  for (final v in e) {
    if (v > bal) profitCount++;
  }
  return McResult(N, S, hitDD, blown, bal, p.dd, cR, exp, beWr,
      profitCount / S, bands, e, m, st);
}
