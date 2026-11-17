// Transcendence — mt5_models.dart
// Model data Jurnal MT5 (backend Flask di VPS) + parser JSON yang tahan
// null/skema berubah. TIDAK ADA logika jaringan di sini (murni data).
//
// Konvensi: angka dari API bisa int/double/null -> dibulatkan lewat _d2(),
// waktu tetap string ISO UTC (tampil apa adanya, diubah ke lokal saat render).

class Mt5Status {
  final String provider;
  final bool enabled;
  final bool connected;
  final String? reason;
  final Map<String, dynamic>? lastSync;
  final Map<String, dynamic>? lastRun;
  final int storedTrades;
  final int storedDeals;

  /// Status provisioning terminal customer ini dari backend
  /// (mt5_terminals.status): 'ready' | 'provisioning' | null.
  /// Dipakai UI utk membedakan "MENYIAPKAN TERMINAL" dari "TERPUTUS".
  final String? terminalStatus;

  const Mt5Status({
    required this.provider,
    required this.enabled,
    required this.connected,
    this.reason,
    this.lastSync,
    this.lastRun,
    required this.storedTrades,
    required this.storedDeals,
    this.terminalStatus,
  });

  factory Mt5Status.fromJson(Map<String, dynamic> j) => Mt5Status(
    provider: (j['provider'] ?? '-') as String,
    enabled: j['enabled'] == true,
    connected: j['connected'] == true,
    reason: j['reason'] as String?,
    lastSync: j['last_sync'] as Map<String, dynamic>?,
    lastRun: j['last_run'] as Map<String, dynamic>?,
    storedTrades: _d2(j['stored']?['trades']).toInt(),
    storedDeals: _d2(j['stored']?['deals']).toInt(),
    terminalStatus: j['terminal'] is Map
        ? (j['terminal']['status'] as String?)
        : null,
  );
}

class Mt5Account {
  final int login;
  final String server;
  final String? company;
  final String currency;
  final int? leverage;
  final double? balance;
  final double? equity;
  final double? marginFree;
  final String? lastSyncAt;

  const Mt5Account({
    required this.login,
    required this.server,
    this.company,
    required this.currency,
    this.leverage,
    this.balance,
    this.equity,
    this.marginFree,
    this.lastSyncAt,
  });

  factory Mt5Account.fromJson(Map<String, dynamic> j) => Mt5Account(
    login: _d2(j['login']).toInt(),
    server: (j['server'] ?? '-') as String,
    company: j['company'] as String?,
    currency: (j['currency'] ?? 'USD') as String,
    leverage: j['leverage'] == null ? null : _d2(j['leverage']).toInt(),
    balance: _opt(j['balance']),
    equity: _opt(j['equity']),
    marginFree: _opt(j['margin_free']),
    lastSyncAt: j['last_sync_at'] as String?,
  );
}

class Mt5Trade {
  final int positionId;
  final String symbol;
  final String? direction; // buy / sell
  final String status; // open / closed
  final double volumeIn;
  final double volumeOut;
  final double? entryPrice;
  final double? exitPrice;
  final String? openTime;
  final String? closeTime;
  final double profit;
  final double commission;
  final double swap;
  final double fee;
  final double netProfit;
  final double? slInitial;
  final double? tpInitial;
  final double? slCurrent;
  final double? tpCurrent;
  final double? priceCurrent;
  final String? exitReason; // sl / tp
  final int? magic;
  final String? comment;
  final double? pips;
  final double? pipDistance;
  final double? rr; // null = N/A (tanpa SL awal)

  const Mt5Trade({
    required this.positionId,
    required this.symbol,
    this.direction,
    required this.status,
    required this.volumeIn,
    required this.volumeOut,
    this.entryPrice,
    this.exitPrice,
    this.openTime,
    this.closeTime,
    required this.profit,
    required this.commission,
    required this.swap,
    required this.fee,
    required this.netProfit,
    this.slInitial,
    this.tpInitial,
    this.slCurrent,
    this.tpCurrent,
    this.priceCurrent,
    this.exitReason,
    this.magic,
    this.comment,
    this.pips,
    this.pipDistance,
    this.rr,
  });

  bool get isOpen => status == 'open';
  bool get isWin => netProfit > 0;

  factory Mt5Trade.fromJson(Map<String, dynamic> j) => Mt5Trade(
    positionId: _d2(j['position_id']).toInt(),
    symbol: (j['symbol'] ?? '?') as String,
    direction: j['direction'] as String?,
    status: (j['status'] ?? 'closed') as String,
    volumeIn: _d2(j['volume_in']),
    volumeOut: _d2(j['volume_out']),
    entryPrice: _opt(j['entry_price']),
    exitPrice: _opt(j['exit_price']),
    openTime: j['open_time_utc'] as String?,
    closeTime: j['close_time_utc'] as String?,
    profit: _d2(j['profit']),
    commission: _d2(j['commission']),
    swap: _d2(j['swap']),
    fee: _d2(j['fee']),
    netProfit: _d2(j['net_profit']),
    slInitial: _opt(j['sl_initial']),
    tpInitial: _opt(j['tp_initial']),
    slCurrent: _opt(j['sl_current']),
    tpCurrent: _opt(j['tp_current']),
    priceCurrent: _opt(j['price_current']),
    exitReason: j['exit_reason'] as String?,
    magic: j['magic'] == null ? null : _d2(j['magic']).toInt(),
    comment: (j['comment'] ?? '') as String,
    pips: _opt(j['pips']),
    pipDistance: _opt(j['pip_distance']),
    rr: _opt(j['rr']),
  );
}

class Mt5Stats {
  final int totalClosed;
  final int wins;
  final int losses;
  final int breakeven;
  final double? winRate; // persen
  final double grossProfit;
  final double grossLoss;
  final double netProfit;
  final double? profitFactor;
  final double? averageWin;
  final double? averageLoss;
  final double? expectancy;
  final double totalVolume;
  final int openTrades;

  const Mt5Stats({
    required this.totalClosed,
    required this.wins,
    required this.losses,
    required this.breakeven,
    this.winRate,
    required this.grossProfit,
    required this.grossLoss,
    required this.netProfit,
    this.profitFactor,
    this.averageWin,
    this.averageLoss,
    this.expectancy,
    required this.totalVolume,
    required this.openTrades,
  });

  factory Mt5Stats.fromJson(Map<String, dynamic> j) => Mt5Stats(
    totalClosed: _d2(j['total_closed']).toInt(),
    wins: _d2(j['wins']).toInt(),
    losses: _d2(j['losses']).toInt(),
    breakeven: _d2(j['breakeven']).toInt(),
    winRate: _opt(j['win_rate']),
    grossProfit: _d2(j['gross_profit']),
    grossLoss: _d2(j['gross_loss']),
    netProfit: _d2(j['net_profit']),
    profitFactor: _opt(j['profit_factor']),
    averageWin: _opt(j['average_win']),
    averageLoss: _opt(j['average_loss']),
    expectancy: _opt(j['expectancy']),
    totalVolume: _d2(j['total_volume']),
    openTrades: _d2(j['open_trades']).toInt(),
  );

  static const empty = Mt5Stats(
    totalClosed: 0,
    wins: 0,
    losses: 0,
    breakeven: 0,
    grossProfit: 0,
    grossLoss: 0,
    netProfit: 0,
    totalVolume: 0,
    openTrades: 0,
  );
}

class Mt5CurvePoint {
  final String? t;
  final String type; // trade / deposit / withdrawal
  final String label;
  final double netCum;
  final double balanceOpsCum;

  const Mt5CurvePoint({
    this.t,
    required this.type,
    required this.label,
    required this.netCum,
    required this.balanceOpsCum,
  });

  factory Mt5CurvePoint.fromJson(Map<String, dynamic> j) => Mt5CurvePoint(
    t: j['t'] as String?,
    type: (j['type'] ?? 'trade') as String,
    label: (j['label'] ?? '') as String,
    netCum: _d2(j['net_cum']),
    balanceOpsCum: _d2(j['balance_ops_cum']),
  );
}

class Mt5Equity {
  final List<Mt5CurvePoint> points;
  final double finalNetCum;
  final double balanceOpsCum;
  final double maxDrawdown;

  /// Persen drawdown dari kurva P/L trading tertutup.
  /// NULL = tidak ada penyebut valid (puncak P/L kumulatif <= 0 — akun
  /// belum pernah untung pada rentang tsb) — UI wajib tampil N/A,
  /// BUKAN 0% yang menyesatkan (Phase 1.1 Temuan 6).
  final double? maxDrawdownPct;
  final String? maxDrawdownAt;

  const Mt5Equity({
    required this.points,
    required this.finalNetCum,
    required this.balanceOpsCum,
    required this.maxDrawdown,
    required this.maxDrawdownPct,
    this.maxDrawdownAt,
  });

  factory Mt5Equity.fromJson(Map<String, dynamic> j) => Mt5Equity(
    points: (j['points'] as List<dynamic>? ?? [])
        .map((e) => Mt5CurvePoint.fromJson(e as Map<String, dynamic>))
        .toList(),
    finalNetCum: _d2(j['final_net_cum']),
    balanceOpsCum: _d2(j['balance_ops_cum']),
    maxDrawdown: _d2(j['max_drawdown']),
    maxDrawdownPct: _opt(j['max_drawdown_pct']),
    maxDrawdownAt: j['max_drawdown_at'] as String?,
  );

  static const empty = Mt5Equity(
    points: [],
    finalNetCum: 0,
    balanceOpsCum: 0,
    maxDrawdown: 0,
    maxDrawdownPct: null,
  );
}

class Mt5TradeList {
  final List<Mt5Trade> items;
  final int page;
  final int pageSize;
  final int total;
  final int pages;

  const Mt5TradeList({
    required this.items,
    required this.page,
    required this.pageSize,
    required this.total,
    required this.pages,
  });

  bool get hasMore => page < pages;

  factory Mt5TradeList.fromJson(Map<String, dynamic> j) => Mt5TradeList(
    items: (j['items'] as List<dynamic>? ?? [])
        .map((e) => Mt5Trade.fromJson(e as Map<String, dynamic>))
        .toList(),
    page: _d2(j['page']).toInt(),
    pageSize: _d2(j['page_size']).toInt(),
    total: _d2(j['total']).toInt(),
    pages: _d2(j['pages']).toInt(),
  );
}

class Mt5TradeDetail {
  final Mt5Trade trade;
  final List<Map<String, dynamic>> deals;
  final List<Map<String, dynamic>> orders;

  const Mt5TradeDetail({
    required this.trade,
    required this.deals,
    required this.orders,
  });

  factory Mt5TradeDetail.fromJson(Map<String, dynamic> j) => Mt5TradeDetail(
    trade: Mt5Trade.fromJson(j['trade'] as Map<String, dynamic>),
    deals: (j['deals'] as List<dynamic>? ?? [])
        .map((e) => e as Map<String, dynamic>)
        .toList(),
    orders: (j['orders'] as List<dynamic>? ?? [])
        .map((e) => e as Map<String, dynamic>)
        .toList(),
  );
}

double _d2(dynamic v) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v) ?? 0;
  return 0;
}

double? _opt(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v);
  return null;
}
