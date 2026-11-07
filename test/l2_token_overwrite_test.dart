// Transcendence — l2_token_overwrite_test.dart
// REGRESI L2 (audit PR #4): token hasil login MASUK TIDAK BOLEH tertimpa
// blok persist di _openSettings() oleh snapshot field "TOKEN MANUAL
// (LANJUTAN)" yang kosong/basi (mt5_page.dart:441 pada 5399169).
//
// Cakupan dua lapis (batasan platform: dart:io HttpClient selalu ditunda
// di widget test — juga di dalam tester.runAsync — sehingga login sukses
// end-to-end tidak dapat digerakkan di widget test; jalur login-sukses
// diuji di LAPISAN UNIT dengan MockClient, pola mt5_auth_test.dart):
//
//   LAPISAN WIDGET (halaman asli, blok persist asli):
//     W1 (K)  SIMPAN + edit eksplisit      -> token manual tersimpan (fix
//                                             branch manualChanged=true)
//     W2 (K2) SIMPAN tanpa edit (pre-fill) -> store TIDAK tersentuh (fix
//                                             branch manualChanged=false —
//                                             cabang yang menyelamatkan
//                                             token login di jalur MASUK,
//                                             karena blok persist & hasil
//                                             pop(true) identik)
//     W3 (S4) MASUK gagal sinkron (URL     -> sheet tetap terbuka, store
//             ilegal — M-A ctor reject)      utuh, URL tidak tersimpan
//     W4 (S5) boot membaca sesi tersimpan  -> halaman mencoba memuat data
//                                             (GAGAL TERHUBUNG — bukan
//                                             BELUM TERHUBUNG), sesi restore
//   LAPISAN UNIT (kelas nyata SessionStore + Mt5AuthApi):
//     U1 (S1)  login sukses fresh -> token BARU tersimpan; + demonstrasi
//              mekanisme L2: saveToken('') membuat readToken null
//     U2 (S2/S3) login sukses dgn token lama pra-ada -> TULIS#1 menang
//     U3 (S4)  login 401 -> store tidak tersentuh
//     U4 (S5)  token bertahan dibaca balik dgn instance SessionStore baru
//              (semantik restart aplikasi)
//
// Lingkungan (disediakan flutter_test_config.dart): flutter_secure_storage
// -> mock channel in-memory ('mt5_session_token'); SharedPreferences ->
// mock in-memory; HTTP -> mock 400 bawaan flutter_test (cukup: satu-satunya
// efek refreshAll di test widget adalah phase=error yang instan & pasti).
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:transcendence/mt5_api.dart';
import 'package:transcendence/mt5_auth.dart';
import 'package:transcendence/mt5_page.dart';

const _email = 'budi@test.id';
const _pass = 'rahasia123';
const _tokenBaru = 'TOKEN-BARU-DARI-SERVER-0123456789abcdef';
const _tokenLama = 'TOKEN-LAMA-BASI-yet-another-stale-token';
const _tokenManual = 'TOK-DITEMPEL-USER-EXPLICIT-EDIT';

/// Bersihkan storage antar test (backing secure channel & SP in-memory
/// bertahan sepanjang sesi test file ini).
Future<void> _resetStorage() async {
  await SessionStore().clear();
  final sp = await SharedPreferences.getInstance();
  await sp.remove('mt5_api_token');
  await sp.remove('mt5_base_url');
}

Future<void> _pumpPage(WidgetTester tester) async {
  await tester.pumpWidget(const MaterialApp(
    home: Scaffold(body: Mt5JournalPage()),
  ));
  await tester.pump(const Duration(milliseconds: 50)); // _boot()
}

/// Isi sesi lama: token di SP legacy (fallback M1) + URL tersimpan.
Future<void> _seedSession(String token, String base) async {
  final sp = await SharedPreferences.getInstance();
  await sp.setString('mt5_api_token', token);
  await sp.setString('mt5_base_url', base);
}

/// Buka sheet (tombol pembuka bergantung fase halaman: unconfigured ->
/// 'ATUR SERVER'; error view -> 'SERVER') dan isi field: 0=URL, 1=email,
/// 2=password, 3=token manual (urutan mt5_page.dart).
Future<void> _openSheetAndFill(WidgetTester tester, String url,
    {String? manualToken, String password = _pass,
     String opener = 'ATUR SERVER'}) async {
  await tester.tap(find.text(opener));
  await tester.pumpAndSettle();
  await tester.enterText(find.byType(TextField).at(0), url);
  await tester.enterText(find.byType(TextField).at(1), _email);
  await tester.enterText(find.byType(TextField).at(2), password);
  if (manualToken != null) {
    await tester.enterText(find.byType(TextField).at(3), manualToken);
  }
  await tester.pump();
}

/// Tekan [button] lalu pump: 400 ms > animasi exit sheet (200 ms) supaya
/// sheet ter-dispose dan cursor-blink Timer TextField-nya ter-cancel; 4 dtk
/// berikutnya membakar Timer snackbar (3 dtk) bila toast muncul.
Future<void> _tapAndSettle(WidgetTester tester, String button) async {
  await tester.tap(find.text(button));
  await tester.pump(const Duration(milliseconds: 400));
  await tester.pump(const Duration(seconds: 4));
  await tester.pump(const Duration(milliseconds: 100));
}

Future<String?> _storedToken() => SessionStore().readToken();

void main() {
  setUp(() async {
    await _resetStorage();
  });

  // ======================================================= LAPISAN WIDGET
  group('L2 widget — blok persist _openSettings (halaman asli)', () {
    testWidgets('W1/K: SIMPAN dgn token manual eksplisit -> tersimpan '
        '(jalur manual utuh)', (tester) async {
      await _pumpPage(tester);
      await _openSheetAndFill(tester, 'https://jurnal.ex.id',
          manualToken: _tokenManual);
      await _tapAndSettle(tester, 'SIMPAN');

      // Fix L2 cabang manualChanged=true: token yang DIKETIK tersimpan.
      expect(await _storedToken(), _tokenManual);
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_base_url'), 'https://jurnal.ex.id');
    });

    testWidgets('W2/K2: SIMPAN TANPA edit (pre-fill token lama) -> store '
        'TIDAK tersentuh', (tester) async {
      await _seedSession(_tokenLama, 'https://jurnal.ex.id');
      await _pumpPage(tester);
      // Sesi ter-seed -> halaman di error view -> tombol pembuka 'SERVER'.
      await _openSheetAndFill(tester, 'https://jurnal.ex.id',
          opener: 'SERVER'); // pre-fill saja
      await _tapAndSettle(tester, 'SIMPAN');

      // Fix L2 cabang manualChanged=false: TIDAK ada tulis ke store. Inilah
      // cabang yang menyelamatkan token hasil login di jalur MASUK — blok
      // persist & hasil pop(true) identik utk SIMPAN dan MASUK.
      expect(await _storedToken(), _tokenLama);
    });

    testWidgets('W3/S4: MASUK gagal (URL ilegal — M-A ctor reject, sinkron) '
        '-> sheet tetap terbuka, store utuh', (tester) async {
      await _seedSession(_tokenLama, 'https://jurnal.ex.id');
      await _pumpPage(tester);
      await _openSheetAndFill(tester, 'http://jurnal.domain.com',
          opener: 'SERVER'); // non-loopback
      await _tapAndSettle(tester, 'MASUK');
      // Sheet TETAP terbuka -> unfocus agar cursor-blink Timer TextField
      // ter-cancel sebelum invarian pending-timer diperiksa.
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump(const Duration(milliseconds: 100));

      // Mt5AuthApi ctor melempar Mt5ApiError (badResponse) -> catch
      // (mt5_page.dart) -> toast; TIDAK pop -> blok persist tidak berjalan.
      expect(await _storedToken(), _tokenLama); // tidak tersentuh
      expect(find.text('MASUK'), findsOneWidget); // sheet tetap terbuka
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_base_url'), 'https://jurnal.ex.id');
    });

    testWidgets('W4/S5: boot membaca sesi tersimpan -> mencoba memuat data '
        '(restore jalan, bukan kembali unconfigured)', (tester) async {
      await _seedSession(_tokenLama, 'https://jurnal.ex.id');
      await _pumpPage(tester);

      // Boot membaca token -> _api terkonfigurasi -> refreshAll -> mock HTTP
      // 400 -> phase=error (bukan unconfigured). Membuktikan readToken &
      // pemulihan sesi pada pembukaan aplikasi.
      await tester.pump(const Duration(milliseconds: 400));
      expect(await _storedToken(), _tokenLama);
      expect(find.text('GAGAL TERHUBUNG'), findsOneWidget);
      expect(find.text('BELUM TERHUBUNG'), findsNothing);
    });

    // ===== TEST REGRESI INTI L2 (merah pada 5399169, hijau setelah fix) =====
    // Login sukses tidak dapat digerakkan di widget test (dart:io HttpClient
    // selalu ditunda oleh FakeAsync flutter_test, bahkan di tester.runAsync).
    // Mekanismenya direproduksi setia: TULIS #1 diinjeksi lewat saveToken()
    // yang SAMA dengan yang dipanggil Mt5AuthApi.login (mt5_auth.dart:190)
    // pada store yang sama, SAAT SHEET TERBUKA — persis urutan sesungguhnya
    // (login sukses -> pop -> blok persist). Sheet ditutup TANPA mengedit
    // field manual = kondisi persis tombol MASUK (tokC = snapshot basi).
    testWidgets('W5/S1: login sukses (tokC kosong) lalu sheet tertutup tanpa '
        'edit -> token BARU TIDAK tertimpa \'\'', (tester) async {
      await _pumpPage(tester);
      await _openSheetAndFill(tester, 'https://jurnal.ex.id');

      // TULIS #1: login sukses — token BARU masuk store (mt5_auth.dart:190).
      await SessionStore().saveToken(_tokenBaru);
      expect(await _storedToken(), _tokenBaru);

      // Sheet ditutup (pop true) tanpa menyentuh field manual.
      await _tapAndSettle(tester, 'SIMPAN');

      // FIX L2: store tetap token BARU. Sebelum fix (5399169): blok persist
      // menulis tokC.text='' -> readToken null -> sesi hilang tiap restart.
      expect(await _storedToken(), _tokenBaru);
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString('mt5_base_url'), 'https://jurnal.ex.id');
    });

    testWidgets('W6/S2+S3: login sukses (tokC pre-fill token lama) lalu sheet '
        'tertutup tanpa edit -> token BARU TIDAK tertimpa token lama',
        (tester) async {
      await _seedSession(_tokenLama, 'https://jurnal.ex.id');
      await _pumpPage(tester);
      await _openSheetAndFill(tester, 'https://jurnal.ex.id',
          opener: 'SERVER'); // tokC TERISI token lama (pre-fill mt5_page.dart:298)

      // TULIS #1: login sukses — token BARU masuk store.
      await SessionStore().saveToken(_tokenBaru);

      // Sheet ditutup tanpa mengedit field manual.
      await _tapAndSettle(tester, 'SIMPAN');

      // FIX L2: token BARU bertahan. Sebelum fix: store kembali tokenLama —
      // rotasi sesi tidak pernah terjadi.
      expect(await _storedToken(), _tokenBaru);
    });
  });

  // ========================================================= LAPISAN UNIT
  group('L2 unit — interplay login vs store (Mt5AuthApi + SessionStore asli)',
      () {
    test('U1/S1: login sukses fresh -> token BARU tersimpan; '
        'saveToken(\'\') membunuh sesi (mekanisme L2)', () async {
      await _resetStorage();
      final store = SessionStore();
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => http.Response(
            jsonEncode({'ok': true, 'token': _tokenBaru}), 200,
            headers: {'content-type': 'application/json'})),
        store: store,
      );

      final tok = await api.login(_email, _pass); // TULIS #1 (mt5_auth.dart:190)

      expect(tok, _tokenBaru);
      expect(await store.readToken(), _tokenBaru);

      // Mekanisme L2 yang diperbaiki: penimpaan dgn '' (snapshot tokC kosong
      // blok persist lama) membuat sesi HILANG — readToken -> null.
      final store2 = SessionStore(); // instance baru = semantik restart
      await store2.saveToken('');
      expect(await store2.readToken(), isNull);
    });

    test('U2/S2+S3: login sukses dgn token lama pra-ada -> token BARU menang '
        '(TULIS #1 tidak tertimpa)', () async {
      await _resetStorage();
      final sp = await SharedPreferences.getInstance();
      await sp.setString('mt5_api_token', _tokenLama);

      final store = SessionStore();
      expect(await store.readToken(), _tokenLama); // migrasi M1 terbaca

      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => http.Response(
            jsonEncode({'ok': true, 'token': _tokenBaru}), 200,
            headers: {'content-type': 'application/json'})),
        store: store,
      );
      await api.login(_email, _pass);

      // Token BARU (bukan pre-fill lama) yang harus bertahan di store.
      // Blok persist pasca-fix tidak menulis apa pun (W2) -> TULIS #1 utuh.
      expect(await store.readToken(), _tokenBaru);
      final store2 = SessionStore(); // baca ulang = restart
      expect(await store2.readToken(), _tokenBaru);
    });

    test('U3/S4: login 401 -> Mt5ApiError, store tidak tersentuh', () async {
      await _resetStorage();
      final store = SessionStore();
      await store.saveToken(_tokenLama);

      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => http.Response(
            jsonEncode({
              'error': {'code': 401, 'message': 'Email atau password salah'}
            }),
            401)),
        store: store,
      );

      await expectLater(
          api.login(_email, 'password-salah'), throwsA(isA<Mt5ApiError>()));
      expect(await store.readToken(), _tokenLama); // utuh (M2)
    });

    test('U4/S5: token hasil login bertahan dibaca balik instance baru '
        '(semantik restart)', () async {
      await _resetStorage();
      final store = SessionStore();
      final api = Mt5AuthApi(
        baseUrl: 'https://jurnal.ex.id',
        client: MockClient((req) async => http.Response(
            jsonEncode({'ok': true, 'token': _tokenBaru}), 200,
            headers: {'content-type': 'application/json'})),
        store: store,
      );
      await api.login(_email, _pass);

      // Pembacaan balik berkali-kali dgn instance SessionStore baru —
      // ekivalen baca secure storage pada pembukaan aplikasi berikutnya.
      for (var i = 0; i < 3; i++) {
        expect(await SessionStore().readToken(), _tokenBaru);
      }
    });
  });
}
