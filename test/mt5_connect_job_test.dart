// Regresi auto-provisioning (keputusan produk #3) — sisi Flutter.
// Server menjawab 202 + job_id/job_secret selama terminal disiapkan;
// klien polling /mt5/connect/status. Token HANYA disimpan setelah server
// menerbitkannya (job 'connected'); kegagalan tidak pernah menyimpan
// token / menampilkan status terhubung.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_auth.dart';

class _Store implements SessionStoreContract {
  String? token;
  int saves = 0;
  @override
  Future<void> clear() async => token = null;
  @override
  Future<String?> readToken() async => token;
  @override
  Future<void> saveToken(String t) async {
    saves++;
    token = t;
  }
}

http.Response _j(Object body, [int status = 200]) => http.Response(
    jsonEncode(body), status,
    headers: {'content-type': 'application/json'});

Mt5AuthApi _api(MockClient c, _Store s) {
  final a = Mt5AuthApi(baseUrl: 'https://srv.test', client: c, store: s);
  a.pollDelay = (_) async {};
  return a;
}

void main() {
  test('202 -> polling -> token disimpan SEKALI setelah connected', () async {
    final store = _Store();
    final statusBodies = <Map<String, dynamic>>[];
    var polls = 0;
    final c = MockClient((req) async {
      if (req.url.path.endsWith('/mt5/connect')) {
        return _j({
          'ok': true, 'state': 'provisioning', 'job_id': 'J1',
          'job_secret': 'S1', 'connection_state': 'provisioning',
          'live_verified': false, 'poll_after_ms': 1000,
        }, 202);
      }
      statusBodies.add(jsonDecode(req.body) as Map<String, dynamic>);
      polls++;
      // selama polling belum final: belum ada token tersimpan
      expect(store.token, isNull);
      if (polls < 3) {
        return _j({'ok': true, 'state': 'starting', 'job_id': 'J1'}, 202);
      }
      return _j({
        'ok': true, 'mode': 'self_link', 'token': 'TOK-1',
        'connection_state': 'connected', 'live_verified': true,
      }, 201);
    });
    final res = await _api(c, store)
        .connectAccount(login: 1, server: 'S', password: 'pw-investor');
    expect(res['connection_state'], 'connected');
    expect(store.token, 'TOK-1');
    expect(store.saves, 1);
    expect(polls, 3);
    for (final b in statusBodies) {
      expect(b, {'job_id': 'J1', 'job_secret': 'S1'});
      expect(jsonEncode(b).contains('pw-investor'), isFalse);
    }
  });

  test('job gagal saat polling -> error, TIDAK ada token', () async {
    final store = _Store();
    final c = MockClient((req) async {
      if (req.url.path.endsWith('/mt5/connect')) {
        return _j({'ok': true, 'state': 'pending', 'job_id': 'J2',
                   'job_secret': 'S2'}, 202);
      }
      return _j({'error': {'code': 401, 'message': 'Verifikasi ke server '
          'broker gagal'}}, 401);
    });
    await expectLater(
        _api(c, store).connectAccount(login: 1, server: 'S', password: 'p'),
        throwsA(isA<Mt5ApiError>()));
    expect(store.token, isNull);
    expect(store.saves, 0);
  });

  test('polling melewati batas -> error network, TIDAK ada token', () async {
    final store = _Store();
    final c = MockClient((req) async => _j(
        {'ok': true, 'state': 'starting', 'job_id': 'J3', 'job_secret': 'x'},
        202));
    final api = _api(c, store)..maxPolls = 3;
    try {
      await api.connectAccount(login: 1, server: 'S', password: 'p');
      fail('harusnya lempar');
    } on Mt5ApiError catch (e) {
      expect(e.kind, Mt5ApiErrorKind.network);
    }
    expect(store.token, isNull);
  });

  test('balasan sukses tanpa token -> error, tidak menyimpan apa pun',
      () async {
    final store = _Store();
    final c = MockClient((req) async =>
        _j({'ok': true, 'connection_state': 'connected'}, 200));
    await expectLater(
        _api(c, store).connectAccount(login: 1, server: 'S', password: 'p'),
        throwsA(isA<Mt5ApiError>()));
    expect(store.saves, 0);
  });
}
