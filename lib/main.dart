// Kalkulator Lot XAUUSD (IDR) — main.dart
// Port 1:1 dari index.html + script.js versi web.
// Semua algoritma, teks, warna, dan tata letak dipertahankan persis sama.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'charts.dart';
import 'engine.dart';
import 'montecarlo.dart';
import 'sticky_table.dart';
import 'tables_build.dart';
import 'ui_kit.dart';

void main() {
  runApp(const TranscendenceApp());
}

class TranscendenceApp extends StatelessWidget {
  const TranscendenceApp({super.key});

  ThemeData _theme(Brightness b) {
    final p = b == Brightness.dark ? Palette.dark : Palette.light;
    return ThemeData(
      brightness: b,
      useMaterial3: true,
      scaffoldBackgroundColor: p.paper,
      cardColor: p.panel,
      dividerColor: p.line,
      splashFactory: InkSparkle.splashFactory,
      colorScheme: ColorScheme(
          brightness: b,
          primary: p.gold,
          onPrimary: p.panel,
          secondary: p.gold,
          onSecondary: p.panel,
          error: p.red,
          onError: Colors.white,
          surface: p.panel,
          onSurface: p.ink,
          surfaceContainerHighest: p.paper),
      textTheme: TextTheme(
        bodyMedium: TextStyle(
            fontFamily: kBodyFont,
            fontSize: 16,
            height: 1.5,
            color: p.ink),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Transcendence',
      theme: _theme(Brightness.light),
      darkTheme: _theme(Brightness.dark),
      themeMode: ThemeMode.system,
      home: const HomeScreen(),
    );
  }
}

// ---------------------------------------------------------------------------

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  // input (nilai default = sama seperti web)
  final balance = TextEditingController(text: '10.000.000');
  final kurs = TextEditingController(text: '17.934');
  final risk = TextEditingController(text: '2');
  final ratio = TextEditingController(text: '2');
  final plan = TextEditingController(text: '25');
  final wr = TextEditingController(text: '50');
  final nTr = TextEditingController(text: '100');
  final cost = TextEditingController(text: '3');
  final ddLim = TextEditingController(text: '30');
  final nSim = TextEditingController(text: '5000');
  bool comp = true;

  // kurs live
  String kursStatus = 'Menyambungkan kurs live…';
  bool kursOk = false, kursBad = false;

  // rules recovery
  int? ruleSel;
  String ruleMode = 'normal'; // normal = rules (10% · 1:2) | rec = recovery (X% · 1:1)
  bool ruleInfo = false;
  String riBal = '–', riBalL = '', riUp = '–', riUpL = '';
  String riAft = '–', riAftL = '', riX = '–', riXL = '';
  String recBtnText = '', riNote = '';

  // hasil hitung (instan) & data tabel (debounce 120ms, seperti web)
  CalcResult calc = const CalcResult(false, 0, 0, 0, 0, [], -1);
  AutoOut auto = const AutoOut(true, '–', false, '', '–', '–', '–', '–');
  String _lastAutoSub = 'Isi maksimal SL (pips) untuk menghitung lot otomatis.';
  MatrixResult? matrix;
  CalcResult? tCalc;
  MatrixResult? tMatrix;
  Timer? _tblTimer, _mcTimer;

  // monte carlo
  McResult? mc;
  String mcVerdictNum = '';
  bool mcVerdictGood = false;
  String mcSub = '';
  List<(String, String)> mcStats = [];

  @override
  void initState() {
    super.initState();
    _restore();
  }

  @override
  void dispose() {
    _tblTimer?.cancel();
    _mcTimer?.cancel();
    for (final c in [
      balance, kurs, risk, ratio, plan, wr, nTr, cost, ddLim, nSim
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  // ---------------- penyimpanan (localStorage → SharedPreferences) ----------

  Future<void> _restore() async {
    final sp = await SharedPreferences.getInstance();
    void fill(String key, Map<String, dynamic> o, List<String> fields) {
      for (final k in fields) {
        final v = o[k];
        if (v != null && v != '') {
          _ctrl(k).text = '$v';
        }
      }
    }

    try {
      final o = jsonDecode(sp.getString('lotxau2') ?? 'null');
      if (o is Map<String, dynamic>) {
        fill('lotxau2', o, ['balance', 'kurs', 'risk', 'ratio', 'plan']);
      }
    } catch (_) {}
    try {
      final o = jsonDecode(sp.getString('lotxau_mc') ?? 'null');
      if (o is Map<String, dynamic>) {
        fill('lotxau_mc', o, ['wr', 'nTr', 'cost', 'ddLim', 'nSim']);
        if (o['comp'] != null) comp = o['comp'] == true;
      }
    } catch (_) {}
    try {
      final s = jsonDecode(sp.getString('lotxau_rules') ?? 'null');
      if (s is Map<String, dynamic> &&
          s['sel'] != null &&
          int.tryParse('${s['sel']}') != null &&
          (int.parse('${s['sel']}') >= 1 &&
              int.parse('${s['sel']}') <= kRuleBalances.length)) {
        ruleSel = int.parse('${s['sel']}');
        ruleMode = s['mode'] == 'rec' ? 'rec' : 'normal';
        _applyRuleMode(persist: false);
      }
    } catch (_) {}
    String mode = 'live';
    try {
      mode = sp.getString('lotxau_kursmode') ?? 'live';
    } catch (_) {}
    if (!mounted) return;
    setState(() {});
    recalcAll();
    if (mode == 'live') {
      fetchLive();
    } else {
      setKursStatus(
          mode == 'fixed'
              ? 'Memakai kurs tetap Rp10.000 (akun fixed rate).'
              : 'Memakai kurs tersimpan. Tekan \u201cPakai kurs sekarang\u201d untuk kurs terbaru.',
          false,
          false);
    }
  }

  TextEditingController _ctrl(String k) => {
        'balance': balance, 'kurs': kurs, 'risk': risk, 'ratio': ratio,
        'plan': plan, 'wr': wr, 'nTr': nTr, 'cost': cost, 'ddLim': ddLim,
        'nSim': nSim,
      }[k]!;

  void _save() {
    SharedPreferences.getInstance().then((sp) {
      sp.setString(
          'lotxau2',
          jsonEncode({
            'balance': balance.text,
            'kurs': kurs.text,
            'risk': risk.text,
            'ratio': ratio.text,
            'plan': plan.text,
          }));
      sp.setString(
          'lotxau_mc',
          jsonEncode({
            'wr': wr.text,
            'nTr': nTr.text,
            'cost': cost.text,
            'ddLim': ddLim.text,
            'nSim': nSim.text,
            'comp': comp,
          }));
      sp.setString('lotxau_rules',
          jsonEncode({'sel': ruleSel ?? '', 'mode': ruleMode}));
    });
  }

  void _saveKursMode(String m) {
    SharedPreferences.getInstance().then((sp) => sp.setString('lotxau_kursmode', m));
  }

  // ---------------- kurs live (API publik, tanpa API key) -------------------

  void setKursStatus(String t, bool ok, bool bad) {
    if (!mounted) return;
    setState(() {
      kursStatus = t;
      kursOk = ok;
      kursBad = bad;
    });
  }

  static const _kursSources = [
    ('open.er-api.com', 'https://open.er-api.com/v6/latest/USD'),
    ('frankfurter.app', 'https://api.frankfurter.app/latest?from=USD&to=IDR'),
  ];

  Future<void> fetchLive() async {
    setKursStatus('Mengambil kurs terbaru…', false, false);
    for (final src in _kursSources) {
      try {
        final res =
            await http.get(Uri.parse(src.$2)).timeout(const Duration(seconds: 8));
        if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
        final d = jsonDecode(res.body);
        final rate = (d['rates']['IDR'] as num?)?.toDouble();
        if (rate == null || !rate.isFinite || rate < 1000) continue;
        kurs.text = _groupPlain(rate.round());
        setKursStatus(
            'Kurs live ${src.$1} · ${_nowId()}', true, false);
        recalcAll();
        return;
      } catch (_) {/* coba sumber berikutnya */}
    }
    setKursStatus(
        'Kurs live gagal diambil (cek koneksi internet). Isi kurs manual atau coba lagi.',
        false,
        true);
  }

  final _bulan = const [
    'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt',
    'Nov', 'Des'
  ];

  String _nowId() {
    final n = DateTime.now();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${n.day} ${_bulan[n.month - 1]}, ${two(n.hour)}.${two(n.minute)}';
  }

  String _groupPlain(int v) {
    final sb = StringBuffer();
    final d = v.abs().toString();
    for (int i = 0; i < d.length; i++) {
      final rem = d.length - i;
      sb.write(d[i]);
      if (rem > 1 && rem % 3 == 1) sb.write('.');
    }
    return (v < 0 ? '-' : '') + sb.toString();
  }

  // ---------------- rules recovery -----------------------------------------

  void _updateRuleFacts() {
    if (ruleSel == null) return;
    final d = ruleData(ruleSel!);
    final xpct = d.x.toStringAsFixed(2).replaceFirst('.', ',');
    riBal = idr(d.b);
    riBalL = 'Balance Rules ${d.n}';
    riUp = '+${idr(d.next)}';
    riUpL = d.n < 35 ? 'Menang +20% → Rules ${d.n + 1}' : 'Menang +20% (di luar tabel)';
    riAft = idr(d.after);
    riAftL = 'Setelah SL 10% (−${idr(d.sl)})';
    riX = '$xpct%';
    riXL = 'X recovery: +${idr(d.diff)} → ${d.prev != null ? 'Rules ${d.n - 1}' : 'balance awal'}';
    if (ruleMode == 'rec') {
      recBtnText = '← Kembali ke mode rules (10% · 1:2)';
      riNote =
          'Mode recovery aktif: balance = sisa setelah SL, risk $xpct dengan rasio 1:1 — sekali menang balik persis ke balance Rules ${d.prev != null ? d.n - 1 : d.n}.';
    } else {
      recBtnText = 'Kalau SL → isi mode recovery ($xpct% · 1:1)';
      riNote = kRuleNoteNormal;
    }
  }

  /// Padanan applyRuleMode(): isi balance/risk/rasio otomatis lalu hitung.
  void _applyRuleMode({bool persist = true}) {
    if (ruleSel == null) return;
    final d = ruleData(ruleSel!);
    if (ruleMode == 'rec') {
      balance.text = _groupPlain(d.after);
      risk.text = d.x.toStringAsFixed(2).replaceFirst('.', ',');
      ratio.text = '1';
    } else {
      balance.text = _groupPlain(d.b);
      risk.text = '10';
      ratio.text = '2';
    }
    _updateRuleFacts();
    ruleInfo = true;
    recalcAll();
    if (persist) _save();
  }

  void _onRuleSelChanged(int? v) {
    setState(() {
      ruleSel = (v == null || v < 1) ? null : v;
      ruleMode = 'normal';
      if (ruleSel != null) {
        _applyRuleMode();
      } else {
        ruleInfo = false;
      }
      _save();
    });
  }

  void _toggleRecMode() {
    if (ruleSel == null) return;
    setState(() {
      ruleMode = ruleMode == 'rec' ? 'normal' : 'rec';
      _applyRuleMode();
    });
  }

  // ---------------- hitung utama (calc) ------------------------------------

  void recalcAll() {
    final bal = numMoney(balance.text);
    final ks = numMoney(kurs.text);
    final rp = numPlain(risk.text);
    final ra = numPlain(ratio.text);
    final pl = numPlain(plan.text);
    final c = computeCalc(bal, ks, rp, ra, pl);
    AutoOut a;
    if (c.ok) {
      a = computeAuto(pl, bal!, ks!, rp!, ra!, c.riskUsd);
      if (!a.empty) _lastAutoSub = a.sub;
      matrix = computeMatrix(bal, ks, rp, pl);
    } else {
      a = AutoOut(true, '–', false, _lastAutoSub, '–', '–', '–', '–');
      matrix = null;
    }
    if (!mounted) return;
    setState(() {
      calc = c;
      auto = a;
    });
    _save();
    // tabel + matriks dirender dengan debounce 120ms (1000 baris)
    _tblTimer?.cancel();
    _tblTimer = Timer(const Duration(milliseconds: 120), () {
      if (!mounted) return;
      setState(() {
        tCalc = calc;
        tMatrix = matrix;
      });
    });
    scheduleMC();
  }

  // Cache specs sel: dibangun sekali per objek hasil hitung (identitas),
  // bukan setiap build — tabel 1000 baris jadi murah saat setState.
  List<MainRowSpec> _mainSpecsCache = const [];
  CalcResult? _mainSpecsSrc;
  List<MainRowSpec> _mainSpecs(CalcResult c) {
    if (identical(_mainSpecsSrc, c)) return _mainSpecsCache;
    _mainSpecsSrc = c;
    return _mainSpecsCache = mainRowSpecs(c);
  }

  List<MatrixRowSpec> _matrixSpecsCache = const [];
  MatrixResult? _matrixSpecsSrc;
  List<MatrixRowSpec> _matrixSpecs(MatrixResult m) {
    if (identical(_matrixSpecsSrc, m)) return _matrixSpecsCache;
    _matrixSpecsSrc = m;
    return _matrixSpecsCache = matrixRowSpecs(m);
  }

  // ---------------- monte carlo (debounce 250ms + isolate) -----------------

  void scheduleMC() {
    _mcTimer?.cancel();
    _mcTimer = Timer(const Duration(milliseconds: 250), _runMcNow);
  }

  Future<void> _runMcNow() async {
    try {
      final bal = numMoney(balance.text);
      final rp = numPlain(risk.text);
      final rr = numPlain(ratio.text);
      final w = numPlain(wr.text);
      final sl = numPlain(plan.text);
      final cst = numPlain(cost.text);
      final dd = numPlain(ddLim.text);
      if (![bal, rp, rr, w].every((v) => v != null && v.isFinite && v > 0) ||
          dd == null ||
          !dd.isFinite) {
        if (!mounted) return;
        // Sama seperti web: MC=null, verdict "Lengkapi input di atas."
        setState(() {
          mc = null;
          mcVerdictNum = '';
          mcSub = '';
          mcStats = [];
        });
        return;
      }
      final p = McParams(
          bal!,
          rp! / 100,
          rr!,
          (sl != null && sl.isFinite && sl > 0) ? sl : 0,
          w! / 100,
          cst != null && cst.isFinite && cst > 0 ? cst : 0,
          dd / 100,
          (int.tryParse('${numPlain(nTr.text)?.round() ?? 100}') ?? 100)
              .clamp(1, 2000)
              .toInt(),
          (int.tryParse('${numPlain(nSim.text)?.round() ?? 5000}') ?? 5000)
              .clamp(200, 20000)
              .toInt(),
          comp);
      McResult res;
      try {
        res = await Isolate.run(() => runMc(p));
      } catch (_) {
        // Cadangan: sebagian device/ROM gagal spawn isolate — hitung langsung
        // di isolate utama supaya simulasi tetap bisa dijalankan.
        res = runMc(p);
      }
      if (!mounted) return;
      final med = pctl(res.ends, .5);
      final pPr = res.pProfit;
      setState(() {
        mc = res;
        mcVerdictNum = '${(pPr * 100).round()}%';
        mcVerdictGood = pPr >= .5;
        mcSub = 'Ekspektasi ${res.exp >= 0 ? '+' : ''}${res.exp.toStringAsFixed(2)}R per trade'
            '${res.cR != 0 ? ' (sudah dipotong biaya ${one(cost.text != '' ? (numPlain(cost.text) ?? 0) : 0)} pips = ${res.cR.toStringAsFixed(2)}R)' : ''}'
            '. Win rate impas: ${(res.beWr * 100).toStringAsFixed(1)}%.'
            '${res.exp < 0 ? ' Dengan angka ini sistemnya rugi dalam jangka panjang.' : ''}';
        mcStats = [
          (idr(med), 'Median balance akhir'),
          ('${med >= res.bal ? '+' : ''}${((med / res.bal - 1) * 100).toStringAsFixed(1)}%',
              'Median return'),
          (idr(pctl(res.ends, .05)), 'Skenario buruk (5%)'),
          (idr(pctl(res.ends, .95)), 'Skenario bagus (95%)'),
          ('${(pctl(res.mdd, .5) * 100).toStringAsFixed(1)}%', 'Median max drawdown'),
          ('${(pctl(res.mdd, .95) * 100).toStringAsFixed(1)}%', 'Drawdown terburuk (95%)'),
          ('${(res.hitDD / res.s * 100).round()}%',
              'Peluang drawdown ≥ ${(res.dd * 100).round()}%'),
          ('${pctl(res.streak, .5).round()}×',
              'Median loss beruntun terpanjang (95%: ${pctl(res.streak, .95).round()}×)'),
          if (res.blown > 0)
            ('${(res.blown / res.s * 100).round()}%', 'Akun habis'),
        ];
      });
    } catch (e) {
      // Semua kegagalan tak terduga ditampilkan — bukan diam tanpa hasil.
      if (!mounted) return;
      setState(() {
        mcVerdictNum = '';
        mcSub = 'Simulasi gagal dijalankan (${e.runtimeType}). '
            'Coba kecilkan jumlah simulasi lalu ubah salah satu input untuk mengulang.';
      });
    }
  }

  // ---------------- export CSV ---------------------------------------------

  Future<void> _exportCsv() async {
    if (tCalc == null || tCalc!.rows.isEmpty) return;
    final buf = StringBuffer('Lot,SL Pips,TP Pips,Risk (IDR),Target (IDR)');
    for (final r in tCalc!.rows) {
      buf.write(
          '\n${lotTxt(r.lot)},${jsNum(r.sl)},${jsNum(r.tp)},${r.r},${r.t}');
    }
    final dir = await getTemporaryDirectory();
    final f = File(
        '${dir.path}/xauusd-lot-${DateTime.now().toIso8601String().substring(0, 10)}.csv');
    await f.writeAsString(buf.toString());
    await SharePlus.instance.share(ShareParams(
        files: [XFile(f.path)],
        title: 'Export CSV',
        subject: 'Kalkulator Lot XAUUSD (IDR)'));
  }

  // ---------------- build ---------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final w = MediaQuery.of(context).size.width;
    final wide = w >= 880;
    final mh = math.min(640.0, MediaQuery.of(context).size.height * 0.7);
    final b0 = bal;

    Widget header() => Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Kalkulator Lot XAUUSD',
                style: TextStyle(
                    fontFamily: kHeadFont,
                    fontWeight: FontWeight.w800,
                    fontSize: clampSize(w * 0.05, 26, 38),
                    height: 1.1,
                    letterSpacing: -0.02 * 16,
                    color: p.ink)),
            const SizedBox(height: 8),
            Text(
                'Dari balance, risiko, dan rasio R:R, tabel menunjukkan jarak SL dan TP (dalam pips) untuk setiap ukuran lot.',
                style: TextStyle(
                    fontFamily: kBodyFont, fontSize: 16, height: 1.5, color: p.muted)),
          ],
        );

    // ---- kartu Rules Recovery ----
    final ruleCard = AppCard(children: [
      const H2('Rules Recovery'),
      const SizedBox(height: 4),
      const Hint(
          'Pilih rules — balance, risk, dan rasio terisi otomatis (pola tabel: naik +20%, SL 10%, recovery 7,41%).'),
      const SizedBox(height: 14),
      _dropdown(),
      if (ruleInfo) ...[
        const SizedBox(height: 12),
        _ruleFacts(p),
        const SizedBox(height: 12),
        PillButton(recBtnText, onTap: _toggleRecMode, gold: true, expanded: true),
        const SizedBox(height: 6),
        Hint(riNote),
      ],
    ]);

    // ---- kartu Input ----
    final inputCard = AppCard(children: [
      const H2('Input'),
      const SizedBox(height: 4),
      LabeledField(
          label: 'Maksimal SL (pips)',
          controller: plan,
          hero: true,
          onChanged: (_) => recalcAll()),
      const SizedBox(height: 6),
      const Hint('Cukup isi jarak SL maksimalmu — lot dihitung otomatis, mentok 10.00 lot.'),
      const SizedBox(height: 14),
      LabeledField(
          label: 'Balance (Rp)',
          controller: balance,
          onChanged: (_) => recalcAll()),
      const SizedBox(height: 6),
      Hint(
          b0 != null && b0.isFinite
              ? 'Terbaca: ${idr(b0)}${b0 < 100000 ? ' — pastikan nominalnya benar (bisa ketik 1.500.000 atau 1,5jt)' : ''}'
              : 'Contoh: 10.000.000 atau 10jt',
          color: b0 != null && b0.isFinite && b0 < 100000 ? p.red : null),
      const SizedBox(height: 14),
      LabeledField(
          label: 'Kurs USD/IDR',
          controller: kurs,
          onChanged: (_) {
            _saveKursMode('manual');
            setKursStatus('Memakai kurs yang kamu isi manual.', false, false);
            recalcAll();
          }),
      const SizedBox(height: 8),
      Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          PillButton('Pakai kurs sekarang', onTap: () {
            _saveKursMode('live');
            fetchLive();
          }),
          PillButton('Kurs tetap 10.000', onTap: () {
            kurs.text = '10.000';
            _saveKursMode('fixed');
            setKursStatus('Memakai kurs tetap Rp10.000 (akun fixed rate).', false, false);
            recalcAll();
          }),
        ],
      ),
      const SizedBox(height: 6),
      Hint(kursStatus,
          color: kursBad ? p.red : (kursOk ? p.good : null)),
      const SizedBox(height: 14),
      w <= 340
          ? Column(children: [
              LabeledField(
                  label: 'Risk (% balance)',
                  controller: risk,
                  onChanged: (_) => recalcAll()),
              const SizedBox(height: 14),
              LabeledField(
                  label: 'Rasio R:R (1:?)',
                  controller: ratio,
                  onChanged: (_) => recalcAll()),
            ])
          : Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  child: LabeledField(
                      label: 'Risk (% balance)',
                      controller: risk,
                      onChanged: (_) => recalcAll())),
              const SizedBox(width: 10),
              Expanded(
                  child: LabeledField(
                      label: 'Rasio R:R (1:?)',
                      controller: ratio,
                      onChanged: (_) => recalcAll())),
            ]),
    ]);

    // ---- kotak lot otomatis ----
    final autoBox = _autoBox(p);

    // ---- risk amount / target ----
    final sumRow = Row(children: [
      Expanded(
          child: Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                  color: p.redsoft, borderRadius: BorderRadius.circular(12)),
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Risk amount',
                        style: TextStyle(
                            fontFamily: kBodyFont,
                            fontSize: 13,
                            color: p.muted)),
                    Text(
                        calc.ok ? idr(calc.riskIdr) : '–',
                        style: TextStyle(
                            fontFamily: kHeadFont,
                            fontWeight: FontWeight.w800,
                            fontSize: clampSize(w * 0.05, 20, 26),
                            height: 1.15,
                            color: p.red,
                            fontFeatures: tabular)),
                    Text(
                        calc.ok
                            ? '${usd(calc.riskUsd)} · ${jsNum(numPlain(risk.text) ?? 0)}% balance'
                            : '–',
                        style: TextStyle(
                            fontFamily: kBodyFont,
                            fontSize: 14,
                            color: p.muted,
                            fontFeatures: tabular)),
                  ]))),
      const SizedBox(width: 10),
      Expanded(
          child: Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                  color: p.goodsoft, borderRadius: BorderRadius.circular(12)),
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Target profit',
                        style: TextStyle(
                            fontFamily: kBodyFont,
                            fontSize: 13,
                            color: p.muted)),
                    Text(calc.ok ? idr(calc.tgtIdr) : '–',
                        style: TextStyle(
                            fontFamily: kHeadFont,
                            fontWeight: FontWeight.w800,
                            fontSize: clampSize(w * 0.05, 20, 26),
                            height: 1.15,
                            color: p.good,
                            fontFeatures: tabular)),
                    Text(
                        calc.ok
                            ? '${usd(calc.tgtUsd)} · 1:${ratio.text}'
                            : '–',
                        style: TextStyle(
                            fontFamily: kBodyFont,
                            fontSize: 14,
                            color: p.muted,
                            fontFeatures: tabular)),
                  ]))),
    ]);

    // ---- tabel utama ----
    // Specs sel di-cache per objek data (identitas) supaya setState tetap
    // ringan; widget sel hanya dibuat untuk baris yang terlihat (lazy).
    final mainTable = tCalc == null || !tCalc!.ok
        ? Container(
            height: 42,
            decoration: BoxDecoration(
                color: p.panel,
                border: Border.all(width: 1, color: p.line),
                borderRadius: BorderRadius.circular(12)),
            alignment: Alignment.centerLeft,
            padding: const EdgeInsets.symmetric(horizontal: 10),
            child: Text(
                'Lengkapi balance, kurs, risk, dan rasio dengan angka lebih dari nol.',
                style: TextStyle(fontFamily: kBodyFont, fontSize: 14, color: p.muted)))
        : StickyTable(
            key: const Key('mainTable'),
            cols: mainCols(p),
            rowCount: tCalc!.rows.length,
            cellBuilder: mainCellBuilder(_mainSpecs(tCalc!), p),
            pickRow: tCalc!.pick,
            maxHeight: mh,
            borderColor: p.line,
          );

    // ---- matriks ----
    final matrixTable = (tMatrix == null)
        ? const SizedBox.shrink()
        : StickyTable(
            key: const Key('matrixTable'),
            cols: matrixCols(tMatrix!, b0 ?? 0, p),
            rowCount: kLots.length,
            cellBuilder: matrixCellBuilder(_matrixSpecs(tMatrix!), p),
            pickRow: null,
            maxHeight: mh,
            borderColor: p.line,
            autoScrollToPick: false,
          );

    // ---- monte carlo card ----
    final mcCard = _mcCard(p, w);

    // ---- catatan bawah ----
    final note = SizedBox(
      width: double.infinity,
      child: RichText(
        text: TextSpan(
          style: TextStyle(
              fontFamily: kBodyFont, fontSize: 13, height: 1.5, color: p.muted),
          children: [
            const TextSpan(text: 'Rumus: '),
            TextSpan(
                text: 'SL pips = Risk USD ÷ (Lot × 10)',
                style: TextStyle(
                    fontFamily: 'monospace', fontSize: 13, color: p.ink)),
            const TextSpan(text: ', '),
            TextSpan(
                text: 'TP pips = SL pips × rasio',
                style: TextStyle(
                    fontFamily: 'monospace', fontSize: 13, color: p.ink)),
            const TextSpan(
                text:
                    '. Pip XAUUSD = \$0,10 per oz, 1 lot = 100 oz. Baris yang pudar punya SL di bawah 10 pips — terlalu sempit untuk emas karena spread saja bisa 2–4 pips. Spread, komisi, dan slippage belum dihitung.'),
          ],
        ),
      ),
    );

    final sideColumn = Column(children: [
      ruleCard,
      const SizedBox(height: 16),
      inputCard,
    ]);

    final mainColumn = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          autoBox,
          const SizedBox(height: 14),
          sumRow,
          const SizedBox(height: 18),
          Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Flexible(child: const H2('Rekomendasi setup (0.01 – 10.00)')),
                const SizedBox(width: 10),
                PillButton('Export CSV', onTap: () => _exportCsv()),
              ]),
          const SizedBox(height: 10),
          mainTable,
          const SizedBox(height: 18),
          const H2('SL pips per lot × % risiko'),
          const SizedBox(height: 4),
          const Hint(
              'Jarak SL maksimal (pips) sebelum kerugian mencapai persentase balance tersebut.'),
          const SizedBox(height: 10),
          matrixTable,
          const SizedBox(height: 8),
          const Hint(
              'Kolom berwarna = risk % yang kamu isi. Kotak bergaris emas = lot terbesar yang masih muat untuk rencana SL-mu di tiap persentase.'),
          const SizedBox(height: 26),
          mcCard,
          const SizedBox(height: 18),
          note,
        ]);

    Widget body;
    if (wide) {
      body = Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 1000),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 26, 16, 44),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 340,
                  child: SingleChildScrollView(
                    child: Column(children: [
                      header(),
                      const SizedBox(height: 22),
                      sideColumn,
                    ]),
                  ),
                ),
                const SizedBox(width: 18),
                Expanded(
                  child: SingleChildScrollView(child: mainColumn),
                ),
              ],
            ),
          ),
        ),
      );
    } else {
      body = SingleChildScrollView(
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1000),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 26, 16, 44),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  header(),
                  const SizedBox(height: 22),
                  ...sideColumn.children
                      .expand((e) => [e, const SizedBox(height: 16)]),
                  ...mainColumn.children,
                ],
              ),
            ),
          ),
        ),
      );
    }

    return Scaffold(body: SafeArea(child: body));
  }

  // ---- sub-widget builders ------------------------------------------------

  double? get bal => numMoney(balance.text);

  Widget _dropdown() {
    final p = Palette.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Rules',
            style: TextStyle(fontFamily: kBodyFont, fontSize: 14, color: p.muted)),
        const SizedBox(height: 5),
        DropdownButtonFormField<int?>(
          initialValue: ruleSel ?? -1,
          isDense: true,
          isExpanded: true, // teks panjang: ellipsis, bukan overflow
          dropdownColor: p.panel,
          style: TextStyle(
              fontFamily: kBodyFont,
              fontSize: 16,
              fontWeight: FontWeight.w600,
              color: p.ink),
          decoration: InputDecoration(
            isDense: true,
            filled: true,
            fillColor: p.paper,
            contentPadding:
                const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
            enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(9),
                borderSide: BorderSide(width: 1, color: p.line)),
            focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(9),
                borderSide: BorderSide(width: 2, color: p.gold)),
          ),
          icon: Icon(Icons.arrow_drop_down, color: p.muted),
          items: [
            const DropdownMenuItem<int?>(
                value: -1, child: Text('— Manual (isi sendiri) —')),
            for (int i = 1; i <= kRuleBalances.length; i++)
              DropdownMenuItem<int?>(
                  value: i, child: Text('Rules $i · ${idr(kRuleBalances[i - 1])}')),
          ],
          onChanged: _onRuleSelChanged,
        ),
      ],
    );
  }

  Widget _ruleFacts(Palette p) {
    Widget fact(String bold, String label, Color? color) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        color: p.panel,
        child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(bold,
                  style: TextStyle(
                      fontFamily: kHeadFont,
                      fontWeight: FontWeight.w700,
                      fontSize: 15,
                      color: color ?? p.ink,
                      fontFeatures: tabular)),
              Text(label,
                  style: TextStyle(
                      fontFamily: kBodyFont, fontSize: 12, color: p.muted)),
            ]));
    return Container(
      decoration: BoxDecoration(
          border: Border.all(width: 1, color: p.line),
          borderRadius: BorderRadius.circular(12)),
      clipBehavior: Clip.antiAlias,
      child: Table(
        border: TableBorder(
            horizontalInside: BorderSide(width: 1, color: p.line),
            verticalInside: BorderSide(width: 1, color: p.line)),
        children: [
          TableRow(children: [
            fact(riBal, riBalL, null),
            fact(riUp, riUpL, p.good),
          ]),
          TableRow(children: [
            fact(riAft, riAftL, p.red),
            fact(riX, riXL, p.gold),
          ]),
        ],
      ),
    );
  }

  Widget _autoBox(Palette p) {
    final w = MediaQuery.of(context).size.width;
    final facts = [
      (auto.risk, 'Risiko aktual'),
      (auto.tp, 'TP pips'),
      (auto.tgt, 'Target profit'),
      (auto.pip, 'Nilai per 1 pip'),
    ];
    return Container(
      decoration: BoxDecoration(
          color: p.panel,
          border: Border.all(width: 1, color: p.line),
          borderRadius: BorderRadius.circular(14)),
      clipBehavior: Clip.antiAlias,
      margin: const EdgeInsets.only(bottom: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: double.infinity,
            color: p.goldsoft,
            padding: const EdgeInsets.fromLTRB(18, 16, 18, 16),
            child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Lot otomatis',
                      style: TextStyle(
                          fontFamily: kBodyFont,
                          fontSize: 14,
                          color: p.muted)),
                  Text(auto.big,
                      style: TextStyle(
                          fontFamily: kHeadFont,
                          fontWeight: FontWeight.w800,
                          fontSize: clampSize(w * 0.11, 46, 60),
                          height: 1.0,
                          letterSpacing: -0.03 * 16,
                          color: auto.bad ? p.red : p.gold,
                          fontFeatures: tabular)),
                  const SizedBox(height: 4),
                  Text(auto.sub,
                      style: TextStyle(
                          fontFamily: kBodyFont,
                          fontSize: 14,
                          color: p.muted)),
                ]),
          ),
          if (!auto.empty)
            Container(
              decoration: BoxDecoration(border: Border(top: BorderSide(width: 1, color: p.line))),
              child: Table(
                border: TableBorder(
                    horizontalInside: BorderSide(width: 1, color: p.line),
                    verticalInside: BorderSide(width: 1, color: p.line)),
                children: [
                  TableRow(children: [
                    for (final f in facts.sublist(0, 2)) _factCell(p, f.$1, f.$2),
                  ]),
                  TableRow(children: [
                    for (final f in facts.sublist(2, 4)) _factCell(p, f.$1, f.$2),
                  ]),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _factCell(Palette p, String bold, String label) => Container(
      color: p.panel,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(bold,
                style: TextStyle(
                    fontFamily: kHeadFont,
                    fontWeight: FontWeight.w700,
                    fontSize: 16,
                    color: p.ink,
                    fontFeatures: tabular)),
            Text(label,
                style: TextStyle(
                    fontFamily: kBodyFont, fontSize: 12, color: p.muted)),
          ]));

  Widget _mcCard(Palette p, double w) {
    final narrow = w < 640;
    final nCol = narrow ? 2 : 3;
    final inputs = <Widget>[
      LabeledField(
          label: 'Win rate (%)',
          controller: wr,
          onChanged: (_) => recalcMcOnly()),
      LabeledField(
          label: 'Jumlah trade',
          controller: nTr,
          onChanged: (_) => recalcMcOnly()),
      LabeledField(
          label: 'Spread + komisi (pips)',
          controller: cost,
          onChanged: (_) => recalcMcOnly()),
      LabeledField(
          label: 'Batas drawdown berbahaya (%)',
          controller: ddLim,
          onChanged: (_) => recalcMcOnly()),
      LabeledField(
          label: 'Jumlah simulasi',
          controller: nSim,
          onChanged: (_) => recalcMcOnly()),
    ];
    // grid nCol kolom
    final gridRows = <TableRow>[];
    for (int i = 0; i < inputs.length; i += nCol) {
      gridRows.add(TableRow(children: [
        for (int j = i; j < i + nCol; j++)
          Padding(
              padding: const EdgeInsets.only(right: 10, bottom: 4, top: 14),
              child: j < inputs.length
                  ? inputs[j]
                  : const SizedBox.shrink()),
      ]));
    }
    final statsRows = <TableRow>[];
    for (int i = 0; i < mcStats.length; i += nCol) {
      statsRows.add(TableRow(children: [
        for (int j = i; j < i + nCol; j++)
          Container(
              color: p.panel,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
              child: j < mcStats.length
                  ? Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(mcStats[j].$1,
                            style: TextStyle(
                                fontFamily: kHeadFont,
                                fontWeight: FontWeight.w700,
                                fontSize: 17,
                                color: p.ink,
                                fontFeatures: tabular)),
                        Text(mcStats[j].$2,
                            style: TextStyle(
                                fontFamily: kBodyFont,
                                fontSize: 12,
                                color: p.muted)),
                      ])
                  : null),
      ]));
    }
    final cc = ChartColors(p.line, p.muted, p.band1, p.band2, p.ink, p.red,
        p.gold, p.good);
    return AppCard(children: [
      const H2('Monte Carlo simulation'),
      const SizedBox(height: 4),
      const Hint(
          'Ribuan urutan trade acak dengan balance, risk %, rasio R:R, dan maksimal SL di atas.'),
      Table(
        defaultVerticalAlignment: TableCellVerticalAlignment.top,
        border: TableBorder(
            horizontalInside: BorderSide(width: 1, color: p.line),
            verticalInside: BorderSide(width: 1, color: p.line)),
        children: gridRows,
      ),
      Row(children: [
        Checkbox(
            value: comp,
            activeColor: p.gold,
            onChanged: (v) {
              setState(() => comp = v ?? false);
              recalcMcOnly();
              _save();
            }),
        Expanded(
            child: Text('Compounding (risk % dari balance terkini)',
                style: TextStyle(
                    fontFamily: kBodyFont, fontSize: 14, color: p.ink))),
      ]),
      Text.rich(
        TextSpan(
          text: mcVerdictNum == ''
              ? 'Lengkapi input di atas.'
              : 'Peluang profit setelah ${mc!.n} trade: ',
          style: TextStyle(
              fontFamily: kHeadFont,
              fontWeight: FontWeight.w700,
              fontSize: clampSize(w * 0.042, 19, 24),
              height: 1.25,
              color: p.ink),
          children: [
            if (mcVerdictNum != '')
              TextSpan(
                  text: mcVerdictNum,
                  style: TextStyle(
                      color: mcVerdictGood ? p.good : p.red)),
          ],
        ),
      ),
      if (mcSub != '') ...[const SizedBox(height: 4), Hint(mcSub)],
      const SizedBox(height: 14),
      Container(
          decoration: BoxDecoration(
              border: Border.all(width: 1, color: p.line),
              borderRadius: BorderRadius.circular(12)),
          padding: const EdgeInsets.all(1),
          child: Table(
            border: TableBorder(
                horizontalInside: BorderSide(width: 1, color: p.line),
                verticalInside: BorderSide(width: 1, color: p.line)),
            children: statsRows,
          )),
      const SizedBox(height: 14),
      Text('Perjalanan balance',
          style: TextStyle(
              fontFamily: kHeadFont,
              fontWeight: FontWeight.w700,
              fontSize: 15,
              color: p.ink)),
      const SizedBox(height: 6),
      _legend(p),
      const SizedBox(height: 6),
      if (mc != null) FanChart(mc: mc!, c: cc),
      const SizedBox(height: 14),
      Text('Sebaran balance akhir',
          style: TextStyle(
              fontFamily: kHeadFont,
              fontWeight: FontWeight.w700,
              fontSize: 15,
              color: p.ink)),
      const SizedBox(height: 6),
      if (mc != null) HistChart(mc: mc!, c: cc),
    ]);
  }

  void recalcMcOnly() {
    _save();
    scheduleMC();
  }

  Widget _legend(Palette p) {
    Widget item(Color c, String t) => Row(mainAxisSize: MainAxisSize.min,
        children: [
          Container(
              width: 12, height: 9,
              decoration: BoxDecoration(
                  color: c, borderRadius: BorderRadius.circular(2))),
          const SizedBox(width: 5),
          Text(t,
              style: TextStyle(
                  fontFamily: kBodyFont, fontSize: 12, color: p.muted)),
        ]);
    return Wrap(spacing: 12, runSpacing: 4, children: [
      item(p.band1, '5–95% skenario'),
      item(p.band2, '25–75%'),
      item(p.gold, 'Median'),
      item(p.red, 'Batas drawdown'),
    ]);
  }
}
