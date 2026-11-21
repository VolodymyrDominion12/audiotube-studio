# 009 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [ ] **T1** Ключі конфігу + `SubtitleRules.from_config` (валідація min < max)
- [ ] **T2** Поділ довгого сегмента за часом — SUB-AC1
- [ ] **T3** Пріоритет точок поділу — SUB-AC2
- [ ] **T4** [P] Баланс рядків — SUB-AC3
- [ ] **T5** Вирівнювання часу: cps у тишу, лічильник щільних, закриття коротких пауз — SUB-AC4, AC5, AC6
- [ ] **T6** Властивості на випадкових транскриптах + детермінованість — SUB-AC7, AC8; наявні тести — SUB-AC9
- [ ] **T7** Підключити правила в `transcribe` і `Pipeline._subtitles_path` (SUB-R10), журнал «щільних»
- [ ] **T8** `rebuild_subtitles` + CLI `subtitles`: створення та ідемпотентність — SUB-AC10
- [ ] **T9** `--force` з `.bak` і без змін — SUB-AC11
- [ ] **T10** [P] `--dry-run` — SUB-AC12
- [ ] **T11** [P] Задачі з `video_id`, без транскрипту, невідомий id — SUB-AC13, AC14
- [ ] **T12** docs/04, `config.example.yaml`, README, `specs/README.md`, статус → implemented
