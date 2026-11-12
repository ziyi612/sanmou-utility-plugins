-- Schema exported without rows from the archived battle database.
PRAGMA foreign_keys=ON;

CREATE TABLE battle_formations (
	id INTEGER NOT NULL,
	battle_id INTEGER,
	side VARCHAR(16),
	formation_name VARCHAR(64),
	hero_ids TEXT,
	hero_positions TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(battle_id) REFERENCES battle_records (id)
);

CREATE TABLE battle_heroes (
	id INTEGER NOT NULL,
	battle_id INTEGER,
	hero_id INTEGER,
	side VARCHAR(16),
	position INTEGER,
	level INTEGER,
	star INTEGER,
	troops INTEGER,
	troops_lost INTEGER,
	damage_dealt INTEGER,
	damage_taken INTEGER,
	damage_healed INTEGER,
	kills INTEGER,
	survived BOOLEAN,
	mvp BOOLEAN,
	PRIMARY KEY (id),
	FOREIGN KEY(battle_id) REFERENCES battle_records (id),
	FOREIGN KEY(hero_id) REFERENCES heroes (hero_id)
);

CREATE TABLE battle_records (
	id INTEGER NOT NULL,
	battle_uid VARCHAR(128),
	timestamp BIGINT,
	time_iso DATETIME,
	attacker_id VARCHAR(128),
	attacker_name VARCHAR(128),
	defender_id VARCHAR(128),
	defender_name VARCHAR(128),
	result VARCHAR(32),
	total_rounds INTEGER,
	battle_type VARCHAR(32),
	attacker_troops_total INTEGER,
	defender_troops_total INTEGER,
	attacker_damage_total INTEGER,
	defender_damage_total INTEGER,
	raw_filename VARCHAR(256),
	raw_url TEXT,
	created_at DATETIME,
	PRIMARY KEY (id)
);

CREATE TABLE ce_battle_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_key TEXT NOT NULL UNIQUE,
    source_report TEXT NOT NULL,
    source_dump_dir TEXT NOT NULL,
    candidate_addr TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    time_iso TEXT NOT NULL,
    result TEXT NOT NULL,
    result_code INTEGER NOT NULL,
    rounds INTEGER NOT NULL,
    decode_method TEXT NOT NULL,
    method_status TEXT NOT NULL,
    uid_verified INTEGER NOT NULL DEFAULT 0,
    text_verified INTEGER NOT NULL DEFAULT 0,
    full_battle_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE heroes (
	id INTEGER NOT NULL,
	hero_id INTEGER,
	name VARCHAR(64) NOT NULL,
	rarity VARCHAR(16),
	troop_type VARCHAR(16),
	faction VARCHAR(32),
	default_skills TEXT,
	created_at DATETIME,
	PRIMARY KEY (id)
);

CREATE TABLE report_components(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), name TEXT NOT NULL,
 payload_json TEXT, requested INTEGER, available INTEGER NOT NULL, complete INTEGER,
 status_json TEXT NOT NULL, PRIMARY KEY(snapshot_id,name));

CREATE TABLE report_event_meanings(snapshot_id INTEGER NOT NULL,sequence INTEGER NOT NULL,version TEXT NOT NULL REFERENCES report_semantic_versions(version),event_id TEXT,event_name_zh TEXT,summary_zh TEXT,parameters_json TEXT NOT NULL,status TEXT NOT NULL,source_json TEXT NOT NULL,mapping_json TEXT NOT NULL,PRIMARY KEY(snapshot_id,sequence,version),FOREIGN KEY(snapshot_id,sequence) REFERENCES report_events(snapshot_id,sequence));

CREATE TABLE report_events(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), sequence INTEGER NOT NULL,
 source_pointer TEXT NOT NULL, event_id TEXT, tab TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(snapshot_id,sequence));

CREATE TABLE report_fields(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), pointer TEXT NOT NULL,
 parent_pointer TEXT, field_key TEXT, ordinal INTEGER NOT NULL,
 node_type TEXT NOT NULL, value_json TEXT NOT NULL, scalar_text TEXT,
 PRIMARY KEY(snapshot_id,pointer));

CREATE TABLE report_files(
 id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, sha256 TEXT NOT NULL,
 raw_bytes BLOB NOT NULL, byte_count INTEGER NOT NULL, source_path TEXT NOT NULL,
 parse_status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(namespace,sha256));

CREATE TABLE report_heroes(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 side_id INTEGER NOT NULL REFERENCES report_sides(id), ordinal INTEGER NOT NULL,
 hero_uid TEXT, position INTEGER, hero_name TEXT, level INTEGER,
 troops_initial INTEGER, troops_remaining INTEGER, troops_dead INTEGER, troops_wounded INTEGER,
 raw_json TEXT NOT NULL, UNIQUE(side_id,ordinal));

CREATE TABLE report_id_meanings(snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),pointer TEXT NOT NULL,version TEXT NOT NULL REFERENCES report_semantic_versions(version),kind TEXT NOT NULL,raw_value_json TEXT NOT NULL,name_zh TEXT,mapped INTEGER NOT NULL,status TEXT NOT NULL,source_json TEXT NOT NULL,mapping_json TEXT NOT NULL,PRIMARY KEY(snapshot_id,pointer,version));

CREATE TABLE report_imports(
 id INTEGER PRIMARY KEY, file_id INTEGER REFERENCES report_files(id),
 namespace TEXT NOT NULL, source_path TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, imported_at TEXT NOT NULL);

CREATE TABLE report_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE report_records(
 id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, battle_uid TEXT NOT NULL,
 current_snapshot_id INTEGER REFERENCES report_snapshots(id),
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(namespace,battle_uid));

CREATE TABLE report_semantic_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);

CREATE TABLE report_semantic_runs(snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),version TEXT NOT NULL REFERENCES report_semantic_versions(version),status TEXT NOT NULL,error TEXT,id_count INTEGER NOT NULL DEFAULT 0,mapped_id_count INTEGER NOT NULL DEFAULT 0,event_count INTEGER NOT NULL DEFAULT 0,mapped_event_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(snapshot_id,version));

CREATE TABLE report_semantic_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1),active_version TEXT NOT NULL REFERENCES report_semantic_versions(version));

CREATE TABLE report_semantic_versions(version TEXT PRIMARY KEY,sources_json TEXT NOT NULL,created_at TEXT NOT NULL);

CREATE TABLE report_sides(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 side TEXT NOT NULL, army_uid TEXT, player_uid TEXT, player_name TEXT, formation_id TEXT,
 morale INTEGER, raw_json TEXT NOT NULL, UNIQUE(snapshot_id,side));

CREATE TABLE report_skills(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 hero_id INTEGER NOT NULL REFERENCES report_heroes(id), ordinal INTEGER NOT NULL,
 skill_uid TEXT, level INTEGER, order_level INTEGER, raw_json TEXT NOT NULL,
 UNIQUE(hero_id,ordinal));

CREATE TABLE report_snapshots(
 id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES report_records(id),
 file_id INTEGER NOT NULL REFERENCES report_files(id), document_index INTEGER NOT NULL,
 payload_json TEXT NOT NULL, source TEXT, capture_version INTEGER,
 completeness TEXT NOT NULL, quality_json TEXT NOT NULL, issues_json TEXT NOT NULL,
 event_count INTEGER NOT NULL, field_count INTEGER NOT NULL, imported_at TEXT NOT NULL,
 UNIQUE(file_id,document_index));

CREATE TABLE skill_usages (
	id INTEGER NOT NULL,
	battle_id INTEGER,
	battle_hero_id INTEGER,
	skill_name VARCHAR(128),
	skill_type VARCHAR(32),
	casts INTEGER,
	total_damage INTEGER,
	total_heal INTEGER,
	targets_hit INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(battle_id) REFERENCES battle_records (id),
	FOREIGN KEY(battle_hero_id) REFERENCES battle_heroes (id)
);

CREATE INDEX ix_battle_formations_battle_id ON battle_formations (battle_id);

CREATE INDEX ix_battle_heroes_battle_id ON battle_heroes (battle_id);

CREATE UNIQUE INDEX ix_battle_records_battle_uid ON battle_records (battle_uid);

CREATE UNIQUE INDEX ix_heroes_hero_id ON heroes (hero_id);

CREATE INDEX ix_skill_usages_battle_hero_id ON skill_usages (battle_hero_id);

CREATE INDEX ix_skill_usages_battle_id ON skill_usages (battle_id);

CREATE INDEX report_events_id ON report_events(event_id);

CREATE INDEX report_fields_key ON report_fields(field_key,node_type);

CREATE INDEX report_id_meanings_lookup ON report_id_meanings(kind,raw_value_json,version);

CREATE VIEW report_current_event_meanings AS SELECT r.namespace,r.battle_uid,m.* FROM report_records r JOIN report_event_meanings m ON m.snapshot_id=r.current_snapshot_id JOIN report_semantic_settings a ON a.singleton=1 AND a.active_version=m.version JOIN report_semantic_runs x ON x.snapshot_id=m.snapshot_id AND x.version=m.version AND x.status='ready';

CREATE VIEW report_current_events AS
 SELECT r.namespace,r.battle_uid,e.* FROM report_records r
 JOIN report_events e ON e.snapshot_id=r.current_snapshot_id;

CREATE VIEW report_current_fields AS
 SELECT r.namespace,r.battle_uid,f.* FROM report_records r
 JOIN report_fields f ON f.snapshot_id=r.current_snapshot_id;

CREATE VIEW report_current_heroes AS
 SELECT r.namespace,r.battle_uid,h.* FROM report_records r
 JOIN report_heroes h ON h.snapshot_id=r.current_snapshot_id;

CREATE VIEW report_current_id_meanings AS SELECT r.namespace,r.battle_uid,m.* FROM report_records r JOIN report_id_meanings m ON m.snapshot_id=r.current_snapshot_id JOIN report_semantic_settings a ON a.singleton=1 AND a.active_version=m.version JOIN report_semantic_runs x ON x.snapshot_id=m.snapshot_id AND x.version=m.version AND x.status='ready';

CREATE VIEW report_current_sides AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_sides s ON s.snapshot_id=r.current_snapshot_id;

CREATE VIEW report_current_skills AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_skills s ON s.snapshot_id=r.current_snapshot_id;

CREATE VIEW report_current_snapshots AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_snapshots s ON s.id=r.current_snapshot_id;
