// Transcendence — main.dart
// Kalkulator Lot XAUUSD (IDR) dengan UI "Obsidian Precision".
//
// CATATAN UI: tampilan diganti total mengikuti desain stitch
// (5 tab + bottom nav, dark obsidian, Geist + JetBrains Mono).
// SEMUA ALGORITMA & STATE LOGIC TIDAK DIUBAH — disalin apa adanya
// dari versi sebelumnya (engine.dart, montecarlo.dart, alur recalc,
// debounce, isolate Monte Carlo, kurs live, rules recovery, CSV).
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'charts.dart';
import 'engine.dart';
import 'journal.dart';
import 'montecarlo.dart';
import 'mt5_page.dart';
import 'sticky_table.dart';
import 'tables_build.dart';
import 'theme.dart';
import 'ui/app_theme.dart';
import 'ui_kit.dart';

void main() {
  runApp(const TranscendenceApp());
}

class TranscendenceApp extends StatefulWidget {
  const TranscendenceApp({super.key});

  @override
  State<TranscendenceApp> createState() => _TranscendenceAppState();
}

class _TranscendenceAppState extends State<TranscendenceApp> {
  @override
  void initState() {
    super.initState();
    themeController.load(); // muat preferensi tema tersimpan
  }

  @override
  Widget build(BuildContext context) {
    // Reaktif: MaterialApp dibangun ulang tiap ThemeMode berubah.
    return ListenableBuilder(
      listenable: themeController,
      builder: (context, _) => MaterialApp(
        title: 'Transcendence',
        debugShowCheckedModeBanner: false,
        theme: _lightTheme(),
        darkTheme: _darkTheme(),
        themeMode: themeController.mode,
        home: const HomeScreen(),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Tema global — dua varian dari design token Palette (ui_kit.dart).
// Tidak ada warna hardcoded di sini; semuanya dari token agar light/dark
// konsisten dan perubahan tema langsung terlihat tanpa restart.
// ---------------------------------------------------------------------------

ThemeData _darkTheme() => appDarkTheme();

ThemeData _lightTheme() => appLightTheme();

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

  // navigasi tab (UI baru: 0 = Kalkulator, 1 = Matrix Risiko, 2 = Simulasi)
  int tab = 0;
  int matrixMode =
      0; // sub-tab di halaman Matrix: 0 = Rekomendasi, 1 = % Risiko
  bool kursOpen = false; // lipatan Parameter Kurs & Saldo

  // kurs live
  String kursStatus = 'Menyambungkan kurs live…';
  bool kursOk = false, kursBad = false;

  // rules recovery
  int? ruleSel;
  String ruleMode =
      'normal'; // normal = rules (10% · 1:2) | rec = recovery (X% · 1:1)
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
      balance,
      kurs,
      risk,
      ratio,
      plan,
      wr,
      nTr,
      cost,
      ddLim,
      nSim,
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
        mode == 'fixed' ? 'Memakai kurs tetap Rp10.000 (akun fixed rate).' : 'Memakai kurs tersimpan. Tekan \u201cLive Market\u201d untuk kurs terbaru.',
        false,
        false,
      );
    }
  }

  TextEditingController _ctrl(String k) => {
    'balance': balance,
    'kurs': kurs,
    'risk': risk,
    'ratio': ratio,
    'plan': plan,
    'wr': wr,
    'nTr': nTr,
    'cost': cost,
    'ddLim': ddLim,
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
        }),
      );
      sp.setString(
        'lotxau_mc',
        jsonEncode({
          'wr': wr.text,
          'nTr': nTr.text,
          'cost': cost.text,
          'ddLim': ddLim.text,
          'nSim': nSim.text,
          'comp': comp,
        }),
      );
      sp.setString(
        'lotxau_rules',
        jsonEncode({'sel': ruleSel ?? '', 'mode': ruleMode}),
      );
    });
  }

  void _saveKursMode(String m) {
    SharedPreferences.getInstance().then(
      (sp) => sp.setString('lotxau_kursmode', m),
    );
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
        final res = await http
            .get(Uri.parse(src.$2))
            .timeout(const Duration(seconds: 8));
        if (res.statusCode != 200) throw Exception('HTTP ${res.statusCode}');
        final d = jsonDecode(res.body);
        final rate = (d['rates']['IDR'] as num?)?.toDouble();
        if (rate == null || !rate.isFinite || rate < 1000) continue;
        kurs.text = _groupPlain(rate.round());
        setKursStatus('Kurs live ${src.$1} · ${_nowId()}', true, false);
        recalcAll();
        return;
      } catch (_) {
        /* coba sumber berikutnya */
      }
    }
    setKursStatus(
      'Kurs live gagal diambil (cek koneksi internet). Isi kurs manual atau coba lagi.',
      false,
      true,
    );
  }

  final _bulan = const [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'Mei',
    'Jun',
    'Jul',
    'Agu',
    'Sep',
    'Okt',
    'Nov',
    'Des',
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
    riUpL = d.n < 35
        ? 'Menang +20% → Rules ${d.n + 1}'
        : 'Menang +20% (di luar tabel)';
    riAft = idr(d.after);
    riAftL = 'Setelah SL 10% (−${idr(d.sl)})';
    riX = '$xpct%';
    riXL =
        'X buangan: +${idr(d.diff)} → ${d.prev != null ? 'Rules ${d.n - 1}' : 'balance awal'}';
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
      // sub "0.00" tidak disimpan — supaya tidak muncul lagi saat state kosong
      if (!a.empty && a.big != '0.00') _lastAutoSub = a.sub;
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
        comp,
      );
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
        mcSub =
            'Ekspektasi ${res.exp >= 0 ? '+' : ''}${res.exp.toStringAsFixed(2)}R per trade'
            '${res.cR != 0 ? ' (sudah dipotong biaya ${one(cost.text != '' ? (numPlain(cost.text) ?? 0) : 0)} pips = ${res.cR.toStringAsFixed(2)}R)' : ''}'
            '. Win rate impas: ${(res.beWr * 100).toStringAsFixed(1)}%.'
            '${res.exp < 0 ? ' Dengan angka ini sistemnya rugi dalam jangka panjang.' : ''}';
        mcStats = [
          (idr(med), 'Median balance akhir'),
          (
            '${med >= res.bal ? '+' : ''}${((med / res.bal - 1) * 100).toStringAsFixed(1)}%',
            'Median return',
          ),
          (idr(pctl(res.ends, .05)), 'Skenario buruk (5%)'),
          (idr(pctl(res.ends, .95)), 'Skenario bagus (95%)'),
          (
            '${(pctl(res.mdd, .5) * 100).toStringAsFixed(1)}%',
            'Median max drawdown',
          ),
          (
            '${(pctl(res.mdd, .95) * 100).toStringAsFixed(1)}%',
            'Drawdown terburuk (95%)',
          ),
          (
            '${(res.hitDD / res.s * 100).round()}%',
            'Peluang drawdown ≥ ${(res.dd * 100).round()}%',
          ),
          (
            '${pctl(res.streak, .5).round()}×',
            'Median loss beruntun terpanjang (95%: ${pctl(res.streak, .95).round()}×)',
          ),
          if (res.blown > 0)
            ('${(res.blown / res.s * 100).round()}%', 'Akun habis'),
        ];
      });
    } catch (e) {
      // Semua kegagalan tak terduga ditampilkan — bukan diam tanpa hasil.
      if (!mounted) return;
      setState(() {
        mcVerdictNum = '';
        mcSub =
            'Simulasi gagal dijalankan (${e.runtimeType}). '
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
        '\n${lotTxt(r.lot)},${jsNum(r.sl)},${jsNum(r.tp)},${r.r},${r.t}',
      );
    }
    final dir = await getTemporaryDirectory();
    final f = File(
      '${dir.path}/xauusd-lot-${DateTime.now().toIso8601String().substring(0, 10)}.csv',
    );
    await f.writeAsString(buf.toString());
    await SharePlus.instance.share(
      ShareParams(
        files: [XFile(f.path)],
        title: 'Export CSV',
        subject: 'Kalkulator Lot XAUUSD (IDR)',
      ),
    );
  }

  // ---------------- util kecil UI -------------------------------------------

  double? get bal => numMoney(balance.text);

  /// Footer angka pembantu di param MC (semua dari input yang sama).
  void recalcMcOnly() {
    _save();
    scheduleMC();
  }

  void _bumpPlan(double delta) {
    final v = numPlain(plan.text)?.round() ?? 0;
    final nv = (v + delta.round()).clamp(1, 9999);
    plan.text = '$nv';
    recalcAll();
  }

  void _bumpNTr(int delta) {
    final v = (numPlain(nTr.text)?.round() ?? 100);
    nTr.text = '${(v + delta).clamp(10, 2000)}';
    recalcMcOnly();
  }

  void _bumpNSim(int delta) {
    final v = (numPlain(nSim.text)?.round() ?? 5000);
    nSim.text = '${(v + delta).clamp(200, 20000)}';
    recalcMcOnly();
  }

  void _resetMcParams() {
    wr.text = '50';
    nTr.text = '100';
    cost.text = '3';
    ddLim.text = '30';
    nSim.text = '5000';
    setState(() => comp = true);
    recalcMcOnly();
  }

  void _applyPlanPreset(String v) {
    plan.text = v;
    recalcAll();
  }

  // ================= BUILD — shell 3 tab (Obsidian Precision) ===============

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final mh = math.min(640.0, MediaQuery.of(context).size.height * 0.7);

    final pages = [
      _calcTab(p, mh), // 0 Kalkulator
      _matrixTab(p, mh), // 1 Matrix Risiko
      _simTab(p, mh), // 2 Simulasi
      const JournalPage(), // 3 Jurnal (manual)
      const Mt5JournalPage(), // 4 Jurnal MT5 (otomatis via backend VPS)
    ];

    return Scaffold(
      backgroundColor: p.canvas,
      body: Column(
        children: [
          SafeArea(bottom: false, child: _header(p)),
          Expanded(
            child: IndexedStack(index: tab, children: pages),
          ),
          PillNavBar(
            index: tab,
            onTap: (i) => setState(() => tab = i),
            items: const [
              PillNavItem(
                Icons.space_dashboard_outlined,
                Icons.space_dashboard,
                'Kalkulator',
              ),
              PillNavItem(
                Icons.grid_view_outlined,
                Icons.grid_view,
                'Matrix Risiko',
              ),
              PillNavItem(Icons.insights_outlined, Icons.insights, 'Simulasi'),
              PillNavItem(Icons.menu_book_outlined, Icons.menu_book, 'Jurnal'),
              PillNavItem(Icons.cloud_sync_outlined, Icons.cloud_sync, 'MT5'),
            ],
          ),
        ],
      ),
    );
  }

  // ---- header: sapaan di dashboard, header halaman di tab lain ----

  Widget _header(Palette p) {
    void openTheme() => showThemePickerSheet(context);
    if (tab == 0) {
      final h = DateTime.now().hour;
      final greet = h < 11
          ? 'Selamat pagi,'
          : (h < 15
                ? 'Selamat siang,'
                : (h < 19 ? 'Selamat sore,' : 'Selamat malam,'));
      return GreetingHeader(
        greeting: greet,
        title: 'Transcendence',
        subtitle: 'LIVE GOLD · XAUUSD · Kalkulator Lot',
        onTheme: openTheme,
      );
    }
    const titles = [
      '',
      'Matrix Risiko',
      'Simulasi Monte Carlo',
      'Jurnal Trading',
      'Jurnal MT5',
    ];
    return PageHeader(
      title: titles[tab],
      onBack: () => setState(() => tab = 0),
      onTheme: openTheme,
    );
  }

  // ================= TAB 0 — KALKULATOR =====================================

  Widget _calcTab(Palette p, double mh) {
    final b0 = bal;
    final d = ruleSel != null ? ruleData(ruleSel!) : null;

    // Nilai tampilan dihitung PERSIS seperti versi lama (hanya dipindah
    // ke kartu baru) — tidak ada rumus baru.
    final tgtVal = d != null
        ? '+${shortIdr(d.next.toDouble())}'
        : (b0 != null ? '+${shortIdr(b0 * 0.2)}' : '–');
    final tgtSub = d != null
        ? (d.n < 35 ? 'Ke Rule ${d.n + 1}' : 'Di luar tabel')
        : 'Pola +20%';
    final slVal = d != null
        ? '−${shortIdr(d.sl.toDouble())}'
        : (b0 != null ? '−${shortIdr(b0 * 0.1)}' : '–');
    final slSub = d != null
        ? 'Sisa ${shortIdr(d.after.toDouble())}'
        : 'Pola −10%';
    final bgVal = d != null ? riX : '–';
    final bgSub = d != null ? '+${shortIdr(d.diff.toDouble())}' : 'Pilih Rule';

    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // PENTING: jangan pakai CrossAxisAlignment.stretch di dalam
          // SingleChildScrollView (tinggi tak terbatas → constraint
          // h=Infinity menggagalkan layout, bug sama seperti commit d2d847b).
          _stageBanner(p, d),
          const SizedBox(height: 16),
          _lotHero(p),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: StatCard(
                  label: 'Target +20%',
                  value: tgtVal,
                  valueColor: p.ink,
                  icon: Icons.north_east,
                  iconColor: p.mint,
                  note: tgtSub,
                  noteIcon: Icons.trending_up,
                  noteColor: p.mint,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: StatCard(
                  label: 'SL Maks 10%',
                  value: slVal,
                  valueColor: p.ink,
                  icon: Icons.south_east,
                  iconColor: p.red,
                  note: slSub,
                  noteIcon: Icons.trending_down,
                  noteColor: p.red,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          _listRowCard(
            p,
            icon: Icons.restart_alt,
            iconColor: p.gold,
            title: 'Buangan Rule',
            sub: bgSub,
            value: bgVal,
            valueColor: p.gold,
          ),
          const SizedBox(height: 22),
          _sectionLabel(p, 'Pengaturan Posisi'),
          const SizedBox(height: 12),
          _slInputCard(p),
          const SizedBox(height: 12),
          _kursCard(p, b0),
          const SizedBox(height: 12),
          _recoveryCard(p),
          const SizedBox(height: 18),
          GoldButton(
            'Lihat Tabel Rekomendasi Lot',
            icon: Icons.table_chart_outlined,
            onTap: () => setState(() => tab = 1),
          ),
        ],
      ),
    );
  }

  /// Judul seksi di luar kartu ("Transaction" pada referensi).
  Widget _sectionLabel(Palette p, String text, {Widget? trailing}) => Row(
    children: [
      Expanded(
        child: Text(
          text,
          style: txt(context, 17, w: FontWeight.w600, ls: -0.3),
        ),
      ),
      ?trailing,
    ],
  );

  /// Kartu baris gaya daftar transaksi referensi.
  Widget _listRowCard(
    Palette p, {
    required IconData icon,
    required Color iconColor,
    required String title,
    required String sub,
    required String value,
    required Color valueColor,
  }) {
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 14, 16, 14),
      decoration: cardDecoration(p),
      child: Row(
        children: [
          IconBubble(icon, color: iconColor, size: 42, square: true),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: txt(context, 14, w: FontWeight.w600)),
                const SizedBox(height: 2),
                Text(
                  sub,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: txt(context, 12, color: p.muted),
                ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          Text(
            value,
            style: mono(context, 15, w: FontWeight.w600, color: valueColor),
          ),
        ],
      ),
    );
  }

  // ---- banner trading plan stage (gaya kolom pencarian referensi) ----

  Widget _stageBanner(Palette p, RuleData? d) {
    final b0 = bal;
    final value = ruleSel != null && d != null
        ? 'Rule ${d.n} · ${idr(d.b)}'
        : (b0 != null ? 'Manual · ${shortIdr(b0)}' : 'Manual · isi balance');
    final rec = ruleMode == 'rec' && ruleSel != null;
    return Material(
      color: p.isDark ? p.card : p.surface1,
      borderRadius: BorderRadius.circular(999),
      child: InkWell(
        onTap: _openRuleSheet,
        borderRadius: BorderRadius.circular(999),
        child: Container(
          padding: const EdgeInsets.fromLTRB(8, 8, 10, 8),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(999),
            border: Border.all(width: 1, color: p.line),
          ),
          child: Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: p.goldSolid,
                  shape: BoxShape.circle,
                ),
                alignment: Alignment.center,
                child: Icon(Icons.flag_outlined, size: 19, color: p.onGold),
              ),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'TRADING PLAN STAGE',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontFamily: kBodyFont,
                        fontSize: 10,
                        fontWeight: FontWeight.w600,
                        letterSpacing: 1.0,
                        color: p.muted,
                      ),
                    ),
                    const SizedBox(height: 1),
                    Text(
                      value,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: txt(context, 14.5, w: FontWeight.w600),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
                decoration: BoxDecoration(
                  color: rec ? p.goldsoft : p.goodsoft,
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      rec
                          ? Icons.change_circle_outlined
                          : Icons.shield_outlined,
                      size: 12,
                      color: rec ? p.gold : p.mint,
                    ),
                    const SizedBox(width: 4),
                    Text(
                      rec ? 'RECOVERY 1:1' : 'COMPOUNDING',
                      style: TextStyle(
                        fontFamily: kBodyFont,
                        fontSize: 9.5,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.6,
                        color: rec ? p.gold : p.mint,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 4),
              Icon(Icons.expand_more, size: 20, color: p.muted),
            ],
          ),
        ),
      ),
    );
  }

  // ---- hero: recommended lot size ----

  Widget _lotHero(Palette p) {
    final empty = auto.empty;
    final bad = auto.bad;
    String riskVal = '–',
        riskSub = '',
        tpVal = '–',
        tpSub = '',
        rrVal = '1 : –',
        rrSub = '';
    if (!empty && auto.risk != '–') {
      final s = auto.risk;
      final cut = s.lastIndexOf(' (');
      if (cut > 0) {
        riskVal = s.substring(0, cut);
        riskSub = s.substring(cut + 2).trim();
        if (riskSub.endsWith(')')) {
          riskSub = riskSub.substring(0, riskSub.length - 1);
          final pct =
              double.tryParse(
                riskSub.replaceAll('%', '').replaceAll(',', '.'),
              ) ??
              0;
          riskSub = '$riskSub% (${pct <= 10 ? 'Safe' : 'Cek Risk'})';
        }
      } else {
        riskVal = s;
      }
      tpVal = auto.tp;
      tpSub = auto.tgt == '–' ? '' : '+${auto.tgt}';
      rrVal = '1 : ${ratio.text.isEmpty ? '?' : ratio.text}';
      rrSub = auto.pip == '–' ? '' : '1 pip ≈ ${auto.pip}';
    }
    final subText = auto.sub.isEmpty ? _lastAutoSub : auto.sub;
    // Paragraf penjelasan "Belum cukup untuk 0.01 lot..." (big == '0.00')
    // disembunyikan dari UI atas permintaan user (dianggap tulisan ngga
    // penting). Engine tetap menghasilkan sub — hanya tampilan yang
    // dilewati. Warning merah lot > 10.00 tetap tampil.
    final showSub = auto.big != '0.00';

    return HeroCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: HeroChip(
                    'RECOMMENDED LOT SIZE',
                    icon: Icons.auto_graph,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              if (!empty && !bad)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    color: p.onHero.withAlpha(235),
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Row(
                    children: [
                      PulseDot(p.mintSolid, size: 6),
                      const SizedBox(width: 5),
                      Text(
                        'Safe Floor',
                        style: TextStyle(
                          fontFamily: kMonoFont,
                          fontSize: 10.5,
                          fontWeight: FontWeight.w600,
                          color: p.heroA,
                        ),
                      ),
                    ],
                  ),
                ),
              if (bad)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    color: p.redSolid,
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Row(
                    children: [
                      PulseDot(p.onRed, size: 6),
                      const SizedBox(width: 5),
                      Text(
                        'Melebihi Batas',
                        style: TextStyle(
                          fontFamily: kMonoFont,
                          fontSize: 10.5,
                          fontWeight: FontWeight.w600,
                          color: p.onRed,
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: [
              Text(
                auto.big,
                style: mono(
                  context,
                  52,
                  w: FontWeight.w600,
                  color: p.onHero,
                  ls: -1.6,
                ),
              ),
              const SizedBox(width: 8),
              Text(
                'LOT',
                style: TextStyle(
                  fontFamily: kMonoFont,
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                  letterSpacing: 1.6,
                  color: p.onHeroMuted,
                ),
              ),
            ],
          ),
          if (showSub && bad) ...[
            const SizedBox(height: 4),
            Text(
              subText,
              style: txt(
                context,
                12,
                color: bad ? p.onHero : p.onHeroMuted,
                height: 1.45,
              ),
            ),
          ],
          const SizedBox(height: 14),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: p.onHero.withAlpha(30),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(width: 1, color: p.onHero.withAlpha(36)),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _heroFact(
                  p,
                  'Risiko Aktual',
                  riskVal,
                  riskSub,
                  p.onHero,
                  p.onHeroMuted,
                ),
                const SizedBox(width: 10),
                _heroFact(p, 'Jarak TP', tpVal, tpSub, p.onHero, p.onHeroMuted),
                const SizedBox(width: 10),
                _heroFact(
                  p,
                  'Target R:R',
                  rrVal,
                  rrSub,
                  p.onHero,
                  p.onHeroMuted,
                ),
              ],
            ),
          ),
          const SizedBox(height: 14),
          InkWell(
            onTap: () => setState(() => tab = 1),
            borderRadius: BorderRadius.circular(8),
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Flexible(
                    child: Text(
                      'Lihat Tabel Rekomendasi Lot',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: txt(
                        context,
                        12.5,
                        w: FontWeight.w600,
                        color: p.onHero,
                      ),
                    ),
                  ),
                  const SizedBox(width: 4),
                  Icon(Icons.chevron_right, size: 18, color: p.onHero),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// Lingkaran dekoratif lembut di kartu hero biru.
  Widget _heroOrb(Palette p, double size) => IgnorePointer(
    child: Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: p.onHero.withAlpha(p.isDark ? 18 : 26),
      ),
    ),
  );

  Widget _heroFact(
    Palette p,
    String label,
    String val,
    String sub,
    Color valColor,
    Color subColor,
  ) {
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: txt(context, 11, color: p.onHeroMuted),
          ),
          const SizedBox(height: 3),
          Text(
            val,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: mono(context, 13.5, w: FontWeight.w600, color: valColor),
          ),
          if (sub.isNotEmpty) ...[
            const SizedBox(height: 2),
            Text(
              sub,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: txt(context, 10.5, color: subColor.withAlpha(230)),
            ),
          ],
        ],
      ),
    );
  }

  // ---- kartu maksimal SL ----

  Widget _slInputCard(Palette p) {
    final planV = numPlain(plan.text)?.round();
    const presets = ['15', '20', '25', '30', '40'];
    return SectionCard(
      title: 'Maksimal Stop Loss',
      icon: Icons.straighten,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(width: 1, color: p.isDark ? p.line : p.lineSoft),
          ),
          child: Row(
            children: [
              Icon(Icons.straighten, size: 20, color: p.gold),
              const SizedBox(width: 10),
              Expanded(
                child: TextField(
                  controller: plan,
                  onChanged: (_) => recalcAll(),
                  keyboardType: const TextInputType.numberWithOptions(
                    decimal: true,
                  ),
                  inputFormatters: [
                    FilteringTextInputFormatter.allow(RegExp(r'[0-9.,]')),
                  ],
                  style: mono(context, 24, w: FontWeight.w600, ls: -0.4),
                  decoration: const InputDecoration(
                    isDense: true,
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.zero,
                  ),
                  cursorColor: p.goldSolid,
                ),
              ),
              _stepBtn(p, Icons.remove, () => _bumpPlan(-1)),
              const SizedBox(width: 6),
              _stepBtn(p, Icons.add, () => _bumpPlan(1)),
            ],
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            for (int i = 0; i < presets.length; i++) ...[
              if (i > 0) const SizedBox(width: 6),
              PresetChip(
                presets[i],
                active: planV != null && presets[i] == '$planV',
                onTap: () => _applyPlanPreset(presets[i]),
              ),
            ],
          ],
        ),
      ],
    );
  }

  Widget _stepBtn(Palette p, IconData icon, VoidCallback onTap) {
    return Material(
      color: p.isDark ? p.cardHighest : p.card,
      shape: CircleBorder(side: BorderSide(color: p.line)),
      child: InkWell(
        onTap: onTap,
        customBorder: const CircleBorder(),
        child: SizedBox(
          width: 36,
          height: 36,
          child: Icon(icon, size: 18, color: p.ink),
        ),
      ),
    );
  }

  // ---- kartu parameter kurs & saldo (lipatan) ----

  Widget _kursCard(Palette p, double? b0) {
    final kTxt = kurs.text;
    String summary;
    if (kursBad) {
      summary = 'Gagal';
    } else if (kursOk) {
      summary = 'Live $kTxt';
    } else if (kTxt == '10.000') {
      summary = 'Fixed 10.000';
    } else {
      summary = 'Manual $kTxt';
    }
    return Container(
      decoration: BoxDecoration(
        color: p.card,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(width: 1, color: p.isDark ? p.lineSoft : p.line),
        boxShadow: p.cardShadow,
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          InkWell(
            onTap: () => setState(() => kursOpen = !kursOpen),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
              child: Row(
                children: [
                  Container(
                    width: 30,
                    height: 30,
                    decoration: BoxDecoration(
                      color: p.goldsoft,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    alignment: Alignment.center,
                    child: Icon(Icons.tune, size: 16, color: p.gold),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      'Parameter Kurs & Saldo',
                      style: txt(context, 14, w: FontWeight.w500),
                    ),
                  ),
                  Text(
                    summary,
                    style: mono(
                      context,
                      11.5,
                      color: kursBad ? p.red : p.gold,
                      w: FontWeight.w500,
                    ),
                  ),
                  const SizedBox(width: 6),
                  AnimatedRotation(
                    turns: kursOpen ? 0.5 : 0,
                    duration: const Duration(milliseconds: 180),
                    child: Icon(Icons.expand_more, size: 19, color: p.muted),
                  ),
                ],
              ),
            ),
          ),
          AnimatedCrossFade(
            duration: const Duration(milliseconds: 180),
            crossFadeState: kursOpen
                ? CrossFadeState.showSecond
                : CrossFadeState.showFirst,
            firstChild: const SizedBox(width: double.infinity),
            secondChild: Container(
              color: p.surface1.withAlpha(120),
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  LabeledField(
                    label: 'MODAL BERSIH AKUN (IDR)',
                    controller: balance,
                    prefix: 'Rp',
                    onChanged: (_) => recalcAll(),
                    labelTrailing: ruleSel != null
                        ? BadgePill(
                            'RULE $ruleSel',
                            color: p.gold,
                            bg: p.goldsoft,
                          )
                        : null,
                    hint: b0 != null && b0.isFinite
                        ? 'Terbaca: ${idr(b0)}${b0 < 100000 ? ' — pastikan nominalnya benar (bisa ketik 1.500.000 atau 1,5jt)' : ''}'
                        : 'Contoh: 10.000.000 atau 10jt',
                    hintColor: b0 != null && b0.isFinite && b0 < 100000
                        ? p.red
                        : null,
                  ),
                  const SizedBox(height: 12),
                  LabeledField(
                    label: 'KURS ACUAN USD / IDR',
                    controller: kurs,
                    prefix: '1 USD = Rp',
                    onChanged: (_) {
                      _saveKursMode('manual');
                      setKursStatus(
                        'Memakai kurs yang kamu isi manual.',
                        false,
                        false,
                      );
                      recalcAll();
                    },
                  ),
                  const SizedBox(height: 12),
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        child: LabeledField(
                          label: 'RISK (% BALANCE)',
                          controller: risk,
                          onChanged: (_) => recalcAll(),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: LabeledField(
                          label: 'RASIO R:R (1 : ?)',
                          controller: ratio,
                          onChanged: (_) => recalcAll(),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: GhostButton(
                          'Kurs Tetap (10.000)',
                          icon: Icons.lock_outline,
                          onTap: () {
                            kurs.text = '10.000';
                            _saveKursMode('fixed');
                            setKursStatus(
                              'Memakai kurs tetap Rp10.000 (akun fixed rate).',
                              false,
                              false,
                            );
                            recalcAll();
                          },
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: GhostButton(
                          'Live Market',
                          icon: Icons.sync,
                          textColor: p.gold,
                          onTap: () {
                            _saveKursMode('live');
                            fetchLive();
                          },
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    kursStatus,
                    style: txt(
                      context,
                      11.5,
                      color: kursBad ? p.red : (kursOk ? p.good : p.muted),
                      height: 1.45,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ---- kartu mode recovery ----

  Widget _recoveryCard(Palette p) {
    final d = ruleSel != null ? ruleData(ruleSel!) : null;
    Widget fact(String bold, String label, Color? color) => Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: p.surface1,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            bold,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: mono(context, 13, w: FontWeight.w600, color: color ?? p.ink),
          ),
          const SizedBox(height: 1),
          Text(
            label,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: txt(context, 10.5, color: p.muted, height: 1.3),
          ),
        ],
      ),
    );
    return ObsCard(
      children: [
        ToggleRow(
          title: ruleSel != null && d != null
              ? 'Mode Recovery (${d.x.toStringAsFixed(2).replaceFirst('.', ',')}%)'
              : 'Mode Recovery',
          subtitle: 'R:R 1:1 setelah Stop Loss untuk pemulihan',
          value: ruleSel != null && ruleMode == 'rec',
          enabled: ruleSel != null,
          onChanged: (_) => _toggleRecMode(),
        ),
        if (ruleSel == null) ...[
          const SizedBox(height: 6),
          Text(
            'Pilih Rule di banner "Trading Plan Stage" untuk mengaktifkan mode recovery.',
            style: txt(context, 11.5, color: p.muted, height: 1.45),
          ),
        ],
        if (ruleInfo && ruleSel != null) ...[
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(child: fact(riBal, riBalL, null)),
              const SizedBox(width: 8),
              Expanded(child: fact(riUp, riUpL, p.good)),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(child: fact(riAft, riAftL, p.red)),
              const SizedBox(width: 8),
              Expanded(child: fact(riX, riXL, p.gold)),
            ],
          ),
        ],
      ],
    );
  }

  // ================= TAB 1 — MATRIX RISIKO ==================================

  Widget _matrixTab(Palette p, double mh) {
    final b0 = bal;
    final ra = numPlain(ratio.text);
    final pl = numPlain(plan.text);
    final mainTable = tCalc == null || !tCalc!.ok
        ? _emptyTableHint(
            p,
            'Lengkapi balance, kurs, risk, dan rasio dengan angka lebih dari nol.',
          )
        : StickyTable(
            key: const Key('mainTable'),
            cols: mainCols(p),
            rowCount: tCalc!.rows.length,
            cellBuilder: mainCellBuilder(_mainSpecs(tCalc!), p),
            pickRow: tCalc!.pick,
            maxHeight: mh,
            borderColor: p.lineSoft,
          );
    final matrixTable = (tMatrix == null)
        ? _emptyTableHint(
            p,
            'Matriks memakai balance & kurs — isi dulu di tab Kalkulator.',
          )
        : StickyTable(
            key: const Key('matrixTable'),
            cols: matrixCols(tMatrix!, b0 ?? 0, p),
            rowCount: kLots.length,
            cellBuilder: matrixCellBuilder(_matrixSpecs(tMatrix!), p),
            pickRow: null,
            maxHeight: mh,
            borderColor: p.lineSoft,
            autoScrollToPick: false,
          );

    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionCard(
            title: 'Matrix Setup & SL Pips',
            subtitle: 'Rekomendasi ukuran lot vs toleransi SL',
            trailing: GhostButton(
              'CSV',
              icon: Icons.download,
              onTap: _exportCsv,
            ),
            children: [
              // segmented control
              Container(
                padding: const EdgeInsets.all(4),
                decoration: BoxDecoration(
                  color: p.isDark ? p.surface1 : p.track,
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Row(
                  children: [
                    _segTab(
                      p,
                      0,
                      Icons.show_chart,
                      'Rekomendasi 1:${ratio.text}',
                    ),
                    const SizedBox(width: 4),
                    _segTab(p, 1, Icons.tune, 'Matrix % Risiko'),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          _setupCard(p, b0, ra, pl),
          const SizedBox(height: 22),
          _sectionLabel(
            p,
            matrixMode == 0 ? 'Tabel Rekomendasi Lot' : 'Matrix % Risiko',
            trailing: BadgePill('XAUUSD', color: p.gold, bg: p.goldsoft),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(6),
            decoration: cardDecoration(p),
            clipBehavior: Clip.antiAlias,
            child: AnimatedSwitcher(
              duration: const Duration(milliseconds: 160),
              child: KeyedSubtree(
                key: ValueKey(matrixMode),
                child: matrixMode == 0 ? mainTable : matrixTable,
              ),
            ),
          ),
          const SizedBox(height: 14),
          if (tCalc != null && tCalc!.ok) _marginBanner(p),
        ],
      ),
    );
  }

  Widget _segTab(Palette p, int i, IconData icon, String label) {
    final active = matrixMode == i;
    return Expanded(
      child: Material(
        color: active ? p.goldSolid : Colors.transparent,
        borderRadius: BorderRadius.circular(999),
        child: InkWell(
          onTap: () => setState(() => matrixMode = i),
          borderRadius: BorderRadius.circular(999),
          child: Container(
            padding: const EdgeInsets.symmetric(vertical: 10),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(icon, size: 15, color: active ? p.onGold : p.muted),
                const SizedBox(width: 6),
                Flexible(
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontFamily: kBodyFont,
                      fontSize: 12.5,
                      fontWeight: active ? FontWeight.w600 : FontWeight.w500,
                      color: active ? p.onGold : p.muted,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _setupCard(Palette p, double? b0, double? ra, double? pl) {
    final lotOpt = (tCalc != null && tCalc!.ok && tCalc!.pick >= 0)
        ? lotTxt(tCalc!.rows[tCalc!.pick].lot)
        : '–';
    final maksSl = (pl != null && pl.isFinite && pl > 0) ? one(pl) : '–';
    final tgtTp = (pl != null && pl.isFinite && pl > 0 && ra != null)
        ? one(round1dec(pl * ra))
        : '–';
    Widget tile(String label, String value, String unit, Color lc, Color vc) =>
        Expanded(
          child: Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: p.onHero.withAlpha(30),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(width: 1, color: p.onHero.withAlpha(36)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: txt(context, 11, color: lc),
                ),
                const SizedBox(height: 3),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.baseline,
                  textBaseline: TextBaseline.alphabetic,
                  children: [
                    Flexible(
                      child: Text(
                        value,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: mono(
                          context,
                          18,
                          w: FontWeight.w600,
                          color: vc,
                          ls: -0.3,
                        ),
                      ),
                    ),
                    const SizedBox(width: 4),
                    Text(unit, style: txt(context, 10.5, color: p.onHeroMuted)),
                  ],
                ),
              ],
            ),
          ),
        );
    return HeroCard(
      padding: const EdgeInsets.all(18),
      child: Stack(
        children: [
          Column(
            children: [
              Row(
                children: [
                  Expanded(
                    child: Align(
                      alignment: Alignment.centerLeft,
                      child: HeroChip(
                        'SETUP PILIHAN · AKUN ${b0 != null ? shortIdr(b0) : '–'}',
                        icon: Icons.verified_outlined,
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  BadgePill(
                    'R:R 1:${ra ?? '–'}',
                    color: p.heroA,
                    bg: p.onHero.withAlpha(235),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              Row(
                children: [
                  tile('Lot Optimal', lotOpt, 'lot', p.onHeroMuted, p.onHero),
                  const SizedBox(width: 8),
                  tile('Maks SL', maksSl, 'pips', p.onHeroMuted, p.onHero),
                  const SizedBox(width: 8),
                  tile('Target TP', tgtTp, 'pips', p.onHeroMuted, p.onHero),
                ],
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _emptyTableHint(Palette p, String msg) => Container(
    height: 52,
    decoration: BoxDecoration(
      color: p.card,
      border: Border.all(width: 1, color: p.isDark ? p.lineSoft : p.line),
      borderRadius: BorderRadius.circular(16),
    ),
    alignment: Alignment.centerLeft,
    padding: const EdgeInsets.symmetric(horizontal: 12),
    child: Text(msg, style: txt(context, 12.5, color: p.muted)),
  );

  Widget _marginBanner(Palette p) {
    final rp = numPlain(risk.text) ?? 0;
    final frac = (rp / 20).clamp(0.05, 1.0);
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: p.card,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(width: 1, color: p.isDark ? p.lineSoft : p.line),
        boxShadow: p.cardShadow,
      ),
      child: Row(
        children: [
          Container(
            width: 38,
            height: 38,
            decoration: BoxDecoration(
              color: p.goodsoft,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Icon(Icons.shield_outlined, size: 19, color: p.mint),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Proteksi Margin Aktif',
                  style: txt(context, 13.5, w: FontWeight.w600),
                ),
                const SizedBox(height: 1),
                Text(
                  'Max kerugian terukur < ${idr(tCalc!.riskIdr)} / trade',
                  style: txt(context, 11.5, color: p.muted),
                ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          SizedBox(
            width: 64,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(99),
              child: LinearProgressIndicator(
                value: frac,
                minHeight: 6,
                backgroundColor: p.cardHighest,
                valueColor: AlwaysStoppedAnimation(
                  rp <= 10 ? p.mintSolid : p.goldSolid,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ================= TAB 2 — SIMULASI (MONTE CARLO) =========================

  Widget _simTab(Palette p, double mh) {
    final cc = ChartColors(
      p.lineSoft,
      p.muted,
      p.band1,
      p.band2,
      p.ink,
      p.red,
      p.gold,
      p.mint,
    );
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _mcHero(p),
          const SizedBox(height: 14),
          _fanCard(p, cc),
          const SizedBox(height: 12),
          if (mcStats.isNotEmpty) ...[
            const SizedBox(height: 10),
            _sectionLabel(p, 'Ringkasan Skenario'),
            const SizedBox(height: 12),
            _metricTiles(p),
          ],
          const SizedBox(height: 14),
          _paramCard(p),
          const SizedBox(height: 16),
          GoldButton(
            'Jalankan Ulang Simulasi (${groupInt(numPlain(nSim.text)?.round() ?? 5000)}×)',
            icon: Icons.play_arrow,
            onTap: recalcMcOnly,
          ),
          const SizedBox(height: 12),
          if (mc != null) ...[
            SectionCard(
              title: 'Sebaran Balance Akhir',
              subtitle: 'Distribusi saldo di akhir seluruh simulasi',
              children: [HistChart(mc: mc!, c: cc)],
            ),
            const SizedBox(height: 12),
          ],
          _noteCard(p),
        ],
      ),
    );
  }

  String _verdictLabel() {
    final v = mc?.pProfit;
    if (v == null) return 'Menunggu';
    if (v >= .8) return 'Sangat Kuat';
    if (v >= .5) return 'Kuat';
    if (v >= .3) return 'Waspada';
    return 'Lemah';
  }

  Widget _mcHero(Palette p) {
    final has = mc != null && mcVerdictNum != '';
    final good = mcVerdictGood;
    final accent = p.onHero;
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        gradient: p.heroGradient,
        borderRadius: BorderRadius.circular(24),
        boxShadow: [
          BoxShadow(
            color: p.goldSolid.withAlpha(p.isDark ? 38 : 70),
            blurRadius: 26,
            spreadRadius: -10,
            offset: const Offset(0, 10),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          Positioned(right: -70, top: -80, child: _heroOrb(p, 180)),
          Positioned(left: 70, bottom: -100, child: _heroOrb(p, 140)),
          Column(
            children: [
              Row(
                children: [
                  PulseDot(p.onHero, size: 7),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'MONTE CARLO ENGINE',
                      style: labelSm(context, color: p.onHeroMuted),
                    ),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 3,
                    ),
                    decoration: BoxDecoration(
                      color: p.onHero.withAlpha(40),
                      borderRadius: BorderRadius.circular(999),
                    ),
                    child: Text(
                      '${groupInt(numPlain(nSim.text)?.round() ?? 0)} Siklus',
                      style: mono(
                        context,
                        10.5,
                        color: p.onHero,
                        w: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          has
                              ? 'Peluang Profit (${mc!.n} Trade)'
                              : 'Peluang Profit',
                          style: txt(context, 12.5, color: p.onHeroMuted),
                        ),
                        const SizedBox(height: 2),
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.baseline,
                          textBaseline: TextBaseline.alphabetic,
                          children: [
                            Text(
                              has ? mcVerdictNum : '–',
                              style: mono(
                                context,
                                34,
                                w: FontWeight.w600,
                                color: accent,
                                ls: -1,
                              ),
                            ),
                            if (has) ...[
                              const SizedBox(width: 10),
                              Container(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 8,
                                  vertical: 3,
                                ),
                                decoration: BoxDecoration(
                                  color: good
                                      ? p.onHero.withAlpha(235)
                                      : p.redSolid,
                                  borderRadius: BorderRadius.circular(999),
                                ),
                                child: Row(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Icon(
                                      good
                                          ? Icons.trending_up
                                          : Icons.trending_down,
                                      size: 13,
                                      color: good ? p.mintSolid : p.onRed,
                                    ),
                                    const SizedBox(width: 3),
                                    Text(
                                      _verdictLabel(),
                                      style: mono(
                                        context,
                                        10.5,
                                        color: good ? p.heroA : p.onRed,
                                        w: FontWeight.w600,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ],
                          ],
                        ),
                      ],
                    ),
                  ),
                  SizedBox(
                    width: 58,
                    height: 58,
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        SizedBox(
                          width: 58,
                          height: 58,
                          child: CustomPaint(
                            painter: _DonutPainter(
                              frac: has ? mc!.pProfit : 0,
                              track: p.onHero.withAlpha(50),
                              arc: has
                                  ? (good ? p.onHero : p.redSolid)
                                  : p.onHero.withAlpha(50),
                            ),
                          ),
                        ),
                        Icon(Icons.insights, size: 20, color: p.onHero),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.only(top: 10),
                decoration: BoxDecoration(
                  border: Border(
                    top: BorderSide(width: 1, color: p.onHero.withAlpha(40)),
                  ),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.auto_awesome, size: 14, color: p.onHero),
                    const SizedBox(width: 7),
                    Expanded(
                      child: Text(
                        has
                            ? 'Ekspektasi ${mc!.exp >= 0 ? '+' : ''}${mc!.exp.toStringAsFixed(2)}R per trade · Impas (BEP): ${(mc!.beWr * 100).toStringAsFixed(1)}% WR'
                            : 'Lengkapi parameter di bawah untuk menjalankan simulasi.',
                        style: txt(
                          context,
                          11.5,
                          color: p.onHeroMuted,
                          height: 1.45,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              if (has && mcSub != '') ...[
                const SizedBox(height: 4),
                Padding(
                  padding: const EdgeInsets.only(left: 21),
                  child: Text(
                    mcSub,
                    style: txt(
                      context,
                      10.5,
                      color: p.onHeroMuted,
                      height: 1.4,
                    ),
                  ),
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _fanCard(Palette p, ChartColors cc) {
    final m = mc;
    return SectionCard(
      title: 'Perjalanan Saldo',
      subtitle: 'Rentang probabilitas skenario masa depan',
      trailing: Material(
        color: p.goldsoft,
        shape: const CircleBorder(),
        child: InkWell(
          onTap: recalcMcOnly,
          customBorder: const CircleBorder(),
          child: SizedBox(
            width: 38,
            height: 38,
            child: Icon(Icons.shuffle, size: 17, color: p.gold),
          ),
        ),
      ),
      children: [
        if (m != null) ...[
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: p.surface1,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      '${shortIdr(m.bands[m.n][4])} (95%)',
                      style: mono(context, 10.5, color: p.muted),
                    ),
                    Text(
                      'Median: ${shortIdr(m.bands[m.n][2])}',
                      style: mono(
                        context,
                        10.5,
                        color: p.gold,
                        w: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 4),
                FanChart(mc: m, c: cc),
              ],
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 14,
            runSpacing: 6,
            children: [
              _legendItem(p, dot: p.gold, text: 'Median Saldo'),
              _legendItem(p, square: p.band2, text: 'Skenario 5–95%'),
              _legendItem(p, line: p.red, text: 'Batas DD'),
            ],
          ),
        ] else
          SizedBox(
            height: 90,
            child: Center(
              child: Text(
                'Belum ada hasil — jalankan simulasi dulu.',
                style: txt(context, 12, color: p.muted),
              ),
            ),
          ),
      ],
    );
  }

  Widget _legendItem(
    Palette p, {
    Color? dot,
    Color? square,
    Color? line,
    required String text,
  }) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (dot != null)
          Container(
            width: 9,
            height: 9,
            decoration: BoxDecoration(shape: BoxShape.circle, color: dot),
          ),
        if (square != null)
          Container(
            width: 9,
            height: 9,
            decoration: BoxDecoration(
              color: square,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
        if (line != null) Container(width: 12, height: 2, color: line),
        const SizedBox(width: 5),
        Text(text, style: txt(context, 11, color: p.muted)),
      ],
    );
  }

  Widget _metricTiles(Palette p) {
    String? stat(int i) => i < mcStats.length ? mcStats[i].$1 : null;
    Widget tile({
      required IconData icon,
      required Color iconBg,
      required Color iconColor,
      required String label,
      required String value,
      required String sub,
      required Color valueColor,
    }) {
      return StatCard(
        label: label,
        value: value,
        valueColor: valueColor,
        icon: icon,
        iconColor: iconColor,
        note: sub,
      );
    }

    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: tile(
                icon: Icons.payments_outlined,
                iconBg: p.goldsoft,
                iconColor: p.gold,
                label: 'Median Saldo',
                value: stat(0) ?? '–',
                sub: '${stat(1) ?? '–'} Return',
                valueColor: p.gold,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: tile(
                icon: Icons.rocket_launch_outlined,
                iconBg: p.goodsoft,
                iconColor: p.mint,
                label: 'Skenario Bagus (95%)',
                value: stat(3) ?? '–',
                sub: 'Peluang Keberuntungan',
                valueColor: p.mint,
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: tile(
                icon: Icons.gpp_maybe_outlined,
                iconBg: p.redsoft,
                iconColor: p.red,
                label: 'Skenario Buruk (5%)',
                value: stat(2) ?? '–',
                sub: 'Kapital Terproteksi',
                valueColor: p.red,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: tile(
                icon: Icons.trending_down,
                iconBg: p.cardHi,
                iconColor: p.warm,
                label: 'Median Max DD',
                value: stat(4) ?? '–',
                sub: 'Worst 95%: ${stat(5) ?? '–'}',
                valueColor: p.ink,
              ),
            ),
          ],
        ),
        if (mcStats.length > 6) ...[
          const SizedBox(height: 10),
          ObsCard(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            children: [
              for (int i = 6; i < mcStats.length; i++)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 5),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          mcStats[i].$2,
                          style: txt(context, 11.5, color: p.muted),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        mcStats[i].$1,
                        style: mono(
                          context,
                          12,
                          w: FontWeight.w600,
                          color: p.ink,
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _paramCard(Palette p) {
    final wrV = (numPlain(wr.text) ?? 50).clamp(1, 100).toDouble();
    final nTrV = (numPlain(nTr.text)?.round() ?? 100).clamp(10, 2000);
    return SectionCard(
      title: 'Parameter Uji Acak',
      subtitle: 'Atur win rate, jumlah trade & simulasi',
      icon: Icons.tune,
      trailing: GhostButton(
        'Reset',
        icon: Icons.restart_alt,
        textColor: p.muted,
        onTap: _resetMcParams,
      ),
      children: [
        // Win rate slider
        Row(
          children: [
            Expanded(
              child: Text(
                'Win Rate Sistem',
                style: txt(context, 12.5, color: p.muted),
              ),
            ),
            Text(
              '${wrV.round()}%',
              style: mono(context, 13, w: FontWeight.w600, color: p.gold),
            ),
          ],
        ),
        SliderTheme(
          data: SliderTheme.of(context).copyWith(
            trackHeight: 5,
            thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 9),
          ),
          child: Slider(
            value: wrV / 100,
            onChanged: (v) {
              wr.text = '${(v * 100).round()}';
              recalcMcOnly();
            },
          ),
        ),
        const SizedBox(height: 4),
        // stepper nTr & nSim
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: _stepperBox(
                p,
                label: 'Banyak Trade',
                value: '$nTrV Trd',
                onMinus: () => _bumpNTr(-10),
                onPlus: () => _bumpNTr(10),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _stepperBox(
                p,
                label: 'Jumlah Iterasi',
                value: '${groupInt(numPlain(nSim.text)?.round() ?? 5000)}×',
                onMinus: () => _bumpNSim(-500),
                onPlus: () => _bumpNSim(500),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: LabeledField(
                label: 'SPREAD + KOMISI (PIPS)',
                controller: cost,
                onChanged: (_) => recalcMcOnly(),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: LabeledField(
                label: 'BATAS DRAWDOWN (%)',
                controller: ddLim,
                onChanged: (_) => recalcMcOnly(),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: p.cardHi.withAlpha(150),
            borderRadius: BorderRadius.circular(14),
          ),
          child: ToggleRow(
            title: 'Compounding Dinamis',
            subtitle: 'Kalkulasi risk % dari saldo berjalan',
            value: comp,
            onChanged: (v) {
              setState(() => comp = v);
              recalcMcOnly();
              _save();
            },
          ),
        ),
      ],
    );
  }

  Widget _stepperBox(
    Palette p, {
    required String label,
    required String value,
    required VoidCallback onMinus,
    required VoidCallback onPlus,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: txt(context, 12.5, color: p.muted)),
            Text(
              value,
              style: mono(context, 12.5, w: FontWeight.w600, color: p.ink),
            ),
          ],
        ),
        const SizedBox(height: 6),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
          decoration: BoxDecoration(
            color: p.cardHi,
            borderRadius: BorderRadius.circular(14),
          ),
          child: Row(
            children: [
              _miniStep(p, Icons.remove, onMinus),
              const Spacer(),
              Text(value, style: mono(context, 12.5, color: p.ink)),
              const Spacer(),
              _miniStep(p, Icons.add, onPlus),
            ],
          ),
        ),
      ],
    );
  }

  Widget _miniStep(Palette p, IconData icon, VoidCallback onTap) {
    return Material(
      color: p.cardHighest,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: SizedBox(
          width: 26,
          height: 26,
          child: Icon(icon, size: 15, color: p.ink),
        ),
      ),
    );
  }

  Widget _noteCard(Palette p) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: p.track,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(width: 1, color: p.lineSoft),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.verified_user_outlined, size: 16, color: p.gold),
          const SizedBox(width: 10),
          Expanded(
            child: Text.rich(
              TextSpan(
                style: txt(context, 11.5, color: p.muted, height: 1.55),
                children: [
                  const TextSpan(text: 'Rumus: '),
                  TextSpan(
                    text: 'SL pips = Risk USD ÷ (Lot × 10)',
                    style: mono(context, 11, color: p.ink),
                  ),
                  const TextSpan(text: ', '),
                  TextSpan(
                    text: 'TP pips = SL pips × rasio',
                    style: mono(context, 11, color: p.ink),
                  ),
                  const TextSpan(
                    text: '. Pip XAUUSD = \$0,10 per oz, 1 lot = 100 oz. Baris yang pudar punya SL di bawah 10 pips — terlalu sempit untuk emas karena spread saja bisa 2–4 pips. Spread, komisi, dan slippage belum dihitung. Simulasi memodelkan sequence risk beruntun (drawdown streaks) XAUUSD.',
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ---- rule picker (bottom sheet) ----

  void _openRuleSheet() {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      builder: (ctx) {
        final p = Palette.of(ctx);
        return SafeArea(
          child: ConstrainedBox(
            constraints: BoxConstraints(
              maxHeight: MediaQuery.of(ctx).size.height * 0.72,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
                  child: Row(
                    children: [
                      PulseDot(p.gold, size: 7),
                      const SizedBox(width: 9),
                      Expanded(
                        child: Text(
                          'Pilih Trading Plan Stage',
                          style: txt(ctx, 16, w: FontWeight.w600),
                        ),
                      ),
                      IconButton(
                        onPressed: () => Navigator.pop(ctx),
                        icon: Icon(Icons.close, size: 20, color: p.muted),
                      ),
                    ],
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Text(
                    'Balance, risk, dan rasio terisi otomatis dari pola tabel (+20% · SL 10% · recovery X%).',
                    style: txt(ctx, 12, color: p.muted),
                  ),
                ),
                const SizedBox(height: 8),
                Expanded(
                  child: ListView.builder(
                    padding: const EdgeInsets.fromLTRB(12, 4, 12, 16),
                    itemCount: kRuleBalances.length + 1,
                    itemBuilder: (ctx, i) {
                      final manual = i == 0;
                      final n = i; // rules 1..35
                      final selected = manual ? ruleSel == null : ruleSel == n;
                      final d = manual ? null : ruleData(n);
                      return Padding(
                        padding: const EdgeInsets.symmetric(vertical: 3),
                        child: Material(
                          color: selected ? p.cardHi : p.card,
                          borderRadius: BorderRadius.circular(18),
                          child: InkWell(
                            onTap: () {
                              Navigator.pop(ctx);
                              _onRuleSelChanged(manual ? -1 : n);
                            },
                            borderRadius: BorderRadius.circular(18),
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 12,
                                vertical: 10,
                              ),
                              decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(18),
                                border: Border.all(
                                  width: selected ? 1 : 0,
                                  color: selected ? p.gold : Colors.transparent,
                                ),
                              ),
                              child: Row(
                                children: [
                                  Icon(
                                    manual
                                        ? Icons.edit_outlined
                                        : Icons.workspace_premium_outlined,
                                    size: 17,
                                    color: selected ? p.gold : p.muted,
                                  ),
                                  const SizedBox(width: 10),
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                          manual
                                              ? 'Manual (isi sendiri)'
                                              : 'Rule $n',
                                          style: txt(
                                            ctx,
                                            13.5,
                                            w: FontWeight.w600,
                                            color: selected ? p.gold : p.ink,
                                          ),
                                        ),
                                        if (!manual) ...[
                                          const SizedBox(height: 1),
                                          Text(
                                            'Balance ${idr(d!.b)} · SL 10% · recovery ${d.x.toStringAsFixed(2).replaceFirst('.', ',')}%',
                                            style: txt(ctx, 11, color: p.muted),
                                          ),
                                        ],
                                      ],
                                    ),
                                  ),
                                  if (selected)
                                    Icon(
                                      Icons.check_circle,
                                      size: 18,
                                      color: p.gold,
                                    ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}

/// Ring progres peluang profit (donut hero Monte Carlo).
class _DonutPainter extends CustomPainter {
  final double frac; // 0..1
  final Color track;
  final Color arc;
  _DonutPainter({required this.frac, required this.track, required this.arc});

  @override
  void paint(Canvas canvas, Size size) {
    final c = Offset(size.width / 2, size.height / 2);
    final r = size.width / 2 - 4;
    final stroke = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 5
      ..strokeCap = StrokeCap.round
      ..color = track;
    canvas.drawCircle(c, r, stroke);
    final f = frac.clamp(0.0, 1.0);
    if (f > 0) {
      final rect = Rect.fromCircle(center: c, radius: r);
      canvas.drawArc(
        rect,
        -math.pi / 2,
        2 * math.pi * f,
        false,
        stroke..color = arc,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _DonutPainter old) =>
      old.frac != frac || old.arc != arc || old.track != track;
}
