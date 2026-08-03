"""config.yaml と .env のロード・検証。

設定値の参照はすべてこのモジュール経由で行う。欠落・型不正は起動時に
ValidationError として落とし、実行中に None が漏れ出さないようにする。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

SourceRank = Literal["S", "A", "B"]
ListingMode = Literal["crawl", "manual_only"]

DEFAULT_CONFIG_PATH = "config.yaml"


class AppConfig(BaseModel):
    timezone: str = "Asia/Tokyo"
    db_path: Path = Path("data/freming.db")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    def target(self) -> str | Path:
        """接続先。DATABASE_URL があれば PostgreSQL、無ければ SQLite。

        接続文字列はパスワードを含むので config.yaml には置かず、.env と
        実行環境の秘匿値からのみ読む。定期実行（GitHub Actions）と
        審査UIが同じDBを見るために、本番は PostgreSQL を使う。
        """
        return os.environ.get("DATABASE_URL") or self.db_path


class HttpConfig(BaseModel):
    user_agent: str
    request_interval_sec: float = 3.0
    max_concurrency_per_domain: int = 1
    max_concurrency_global: int = 4
    timeout_sec: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 2.0
    respect_robots_txt: bool = True

    @field_validator("request_interval_sec")
    @classmethod
    def _min_interval(cls, v: float) -> float:
        if v < 3.0:
            raise ValueError("request_interval_sec は 3.0 秒以上にすること")
        return v

    @field_validator("max_concurrency_per_domain")
    @classmethod
    def _no_parallel_per_domain(cls, v: int) -> int:
        if v != 1:
            raise ValueError("同一ドメインへの並列アクセスは禁止（max_concurrency_per_domain は 1）")
        return v

    @field_validator("respect_robots_txt")
    @classmethod
    def _must_respect_robots(cls, v: bool) -> bool:
        if not v:
            raise ValueError("robots.txt の尊重は必須（respect_robots_txt を false にはできない）")
        return v

    @field_validator("user_agent")
    @classmethod
    def _contact_in_ua(cls, v: str) -> str:
        if "@" not in v and "http" not in v:
            raise ValueError("User-Agent には連絡先（メールまたはURL）を明記すること")
        return v


class CollectConfig(BaseModel):
    lookback_days: int = 30
    max_items_per_source_per_run: int = 30
    # 記事ページも取得して本文を補うか。false ならフィードの配信内容だけで判定する。
    fetch_article_pages: bool = True
    # 記事ページの取得が連続でこの回数失敗したら、その実行では取得を諦めて
    # フィードの内容だけで判定する（無駄なリクエストを相手に送り続けない）。
    article_fetch_failure_limit: int = 3


class EditorialSource(BaseModel):
    key: str
    name: str
    rank: SourceRank
    enabled: bool = False
    feeds: list[str] = Field(default_factory=list)
    sitemap: str | None = None
    # 掲載記事すべてが売出中の物件であるメディア（販売専門）は、
    # 本文から販売シグナルを探す前提が成り立たないため足切りを行わない。
    # 販売可否の最終判断は従来どおり [2] スコアリングに委ねる。
    assume_for_sale: bool = False
    # URLの正規表現による絞り込み。フィードに物件以外（エージェント紹介、
    # イベント告知など）が混ざるソースで使う。記事を取得する前に判定するため、
    # 相手サイトへの無駄なアクセスも減る。
    url_include: list[str] = Field(default_factory=list)   # 空なら全件通す
    url_exclude: list[str] = Field(default_factory=list)
    # 記事ページを取得するか。None なら collect.fetch_article_pages に従う。
    # Crawl-delay の長いサイトでは false にして、フィード配信分だけで判定する
    # （リクエストがフィード1回で済み、待ち時間がなくなる）。
    fetch_article_pages: bool | None = None

    def url_allowed(self, url: str) -> bool:
        import re

        if any(re.search(pattern, url) for pattern in self.url_exclude):
            return False
        if not self.url_include:
            return True
        return any(re.search(pattern, url) for pattern in self.url_include)


class ListingSource(BaseModel):
    key: str
    name: str
    rank: SourceRank
    mode: ListingMode
    enabled: bool = False
    base_url: str | None = None
    note: str | None = None


class ForSaleSignals(BaseModel):
    keyword_score: int = 1
    price_score: int = 1
    listing_link_score: int = 2
    min_signal_score: int = 2
    # 価格表記は、販売キーワードからこの文字数以内にあるものだけを加点対象にする。
    # 建設費・事業費・チケット代などの金額を売出価格と取り違えないための制約。
    # 0 にすると文中のあらゆる金額を加点対象にする（誤検出が増えるため非推奨）。
    price_requires_keyword_within: int = 200
    keywords: list[str] = Field(default_factory=list)
    price_patterns: list[str] = Field(default_factory=list)
    listing_domains: list[str] = Field(default_factory=list)


class FocusArea(BaseModel):
    country: str
    city: str | None = None
    districts: list[str] = Field(default_factory=list)


class SeriesLabel(BaseModel):
    """連載企画のラベル。

    key はDBに保存される値なので変更しない（既存の行が孤立する）。
    label は表示名なので自由に変えてよい。
    """

    key: str
    label: str


class GenresConfig(BaseModel):
    priority: list[str] = Field(default_factory=list)
    keywords: dict[str, list[str]] = Field(default_factory=dict)


class ScoringWeights(BaseModel):
    story: float
    source_rank: float
    editorial_for_sale_bonus: float
    genre_match: float
    area_match: float
    price: float

    @property
    def total(self) -> float:
        return (
            self.story
            + self.source_rank
            + self.editorial_for_sale_bonus
            + self.genre_match
            + self.area_match
            + self.price
        )


class ScoringThresholds(BaseModel):
    min_to_persist: float = 30.0
    highlight_above: float = 80.0


class FeedbackConfig(BaseModel):
    recent_reasons_in_prompt: int = 30
    tagging_batch_size: int = 10
    rule_candidate_min_hits: int = 3
    # 非承認理由の分類先。表記のゆれを吸収して集計するための固定語彙。
    tags: list[str] = Field(default_factory=lambda: ["other"])

    @field_validator("tags")
    @classmethod
    def _must_have_other(cls, v: list[str]) -> list[str]:
        if "other" not in v:
            raise ValueError(
                "scoring.feedback.tags には other を含めること"
                "（どのタグにも当てはまらない理由の受け皿が必要）"
            )
        return v


class ScoringConfig(BaseModel):
    model: str = "claude-sonnet-5"
    max_tokens: int = 2000
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    summary_max_chars: int = 80
    score_scale: int = 100
    weights: ScoringWeights
    source_rank_score: dict[str, float]
    # 承認済みの実例と、そこから抽出した判断軸。プロンプトに含めて基準を揃える。
    approved_examples: list[str] = Field(default_factory=list)
    approval_notes: list[str] = Field(default_factory=list)
    thresholds: ScoringThresholds = Field(default_factory=ScoringThresholds)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)

    @field_validator("weights")
    @classmethod
    def _weights_sum_to_one(cls, v: ScoringWeights) -> ScoringWeights:
        if abs(v.total - 1.0) > 1e-6:
            raise ValueError(f"scoring.weights の合計は 1.0 にすること（現在 {v.total}）")
        return v


class ReviewUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    page_size: int = 50
    default_filter: str = "pending"
    # カードのサムネイルの一辺（px）。正方形で表示する。
    # 大きいほど写真の判断はしやすいが、1画面に入る件数が減る。
    # 旧名 thumbnail_max_px。テンプレートに渡すだけで使われておらず、
    # 値を変えても何も起きない状態だった。
    thumbnail_px: int = 170


class DeliveryConfig(BaseModel):
    """承認から納品までの自動化。

    審査UI（serve）の中で1本のワーカースレッドが承認済みを拾って納品する。
    並列にはしない。画像取得は相手サイトへのアクセスなので、
    [1] 収集と同じく直列・間隔ありのルールをそのまま守る必要がある。
    """

    auto: bool = True
    # 承認を取りこぼしたときの拾い直し間隔。承認直後はイベントで起こすので、
    # 通常はこの間隔を待たずに納品が始まる。
    poll_interval_sec: float = 30.0
    # 1回の巡回で納品する最大件数。まとめて承認しても、1件ずつ順に処理する。
    batch_limit: int = 5
    # 自動での試行回数の上限。超えたら自動では触らず、審査UIから人が再試行する。
    # 画像が取れない・Driveが落ちている等を延々と叩き続けないための歯止め。
    max_attempts: int = 3
    # 失敗した候補を次に試すまでの待ち時間。
    retry_after_sec: float = 600.0

    @field_validator("poll_interval_sec")
    @classmethod
    def _not_too_frequent(cls, v: float) -> float:
        if v < 5.0:
            raise ValueError("delivery.poll_interval_sec は 5.0 秒以上にすること")
        return v

    @field_validator("max_attempts")
    @classmethod
    def _at_least_once(cls, v: int) -> int:
        if v < 1:
            raise ValueError("delivery.max_attempts は 1 以上にすること")
        return v


class ImagesConfig(BaseModel):
    max_per_property: int = 10
    min_short_edge_px: int = 800
    allowed_content_types: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/webp"]
    )
    work_dir: Path = Path("data/images")


class ProcessConfig(BaseModel):
    output_size: tuple[int, int] = (1080, 1080)
    jpeg_quality: int = 90
    pad_when_aspect_over: float = 2.0
    pad_color: str = "#FFFFFF"
    resample: str = "lanczos"


class DrivePreflight(BaseModel):
    enabled: bool = True
    test_filename: str = ".frmg_write_test"


class DriveRetry(BaseModel):
    max_attempts: int = 5
    backoff_factor: float = 2.0


class DriveConfig(BaseModel):
    # oauth           … OAuthクライアント（デスクトップアプリ）で人のアカウントとして認証。
    #                   組織ポリシーでサービスアカウント鍵を作れない場合はこれを使う。
    # service_account … サービスアカウントのJSON鍵（credentials_path）。
    # adc             … Application Default Credentials。gcloud のログイン、
    #                   Workload Identity 連携、サービスアカウントの権限借用に対応。
    auth_mode: Literal["oauth", "service_account", "adc"] = "oauth"
    enabled: bool = True
    credentials_path: Path = Path("credentials/service-account.json")
    oauth_client_secret_path: Path = Path("credentials/oauth_client.json")
    oauth_token_path: Path = Path("credentials/token.json")
    parent_folder_id: str
    shared_drive_id: str | None = None
    folder_prefix: str = "frmg_ig"
    sequence_digits: int = 3
    meta_filename: str = "meta.txt"
    verify_uploaded_size: bool = True
    preflight: DrivePreflight = Field(default_factory=DrivePreflight)
    retry: DriveRetry = Field(default_factory=DriveRetry)

    @field_validator("parent_folder_id")
    @classmethod
    def _placeholder_not_allowed(cls, v: str) -> str:
        if not v or v.startswith("PUT_"):
            raise ValueError("drive.parent_folder_id に実際のフォルダIDを設定すること")
        return v


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    http: HttpConfig
    collect: CollectConfig = Field(default_factory=CollectConfig)
    editorial_sources: list[EditorialSource] = Field(default_factory=list)
    listing_sources: list[ListingSource] = Field(default_factory=list)
    for_sale_signals: ForSaleSignals = Field(default_factory=ForSaleSignals)
    focus_areas: list[FocusArea] = Field(default_factory=list)
    series: list[SeriesLabel] = Field(default_factory=list)
    genres: GenresConfig = Field(default_factory=GenresConfig)
    scoring: ScoringConfig
    review_ui: ReviewUIConfig = Field(default_factory=ReviewUIConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    process: ProcessConfig = Field(default_factory=ProcessConfig)
    drive: DriveConfig

    # --- 秘匿値は .env からのみ ---
    @property
    def anthropic_api_key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY が未設定です。.env.example を .env にコピーして設定してください。"
            )
        return key

    # --- 便利アクセサ ---
    def editorial_source(self, key: str) -> EditorialSource | None:
        return next((s for s in self.editorial_sources if s.key == key), None)

    def listing_source(self, key: str) -> ListingSource | None:
        return next((s for s in self.listing_sources if s.key == key), None)

    def series_label(self, key: str | None) -> str | None:
        """保存されている key から表示名を引く。未知の key は key のまま返す。

        config.yaml から企画を消しても、その企画で納品済みの行が
        表示できなくなることはない。
        """
        if not key:
            return None
        found = next((s.label for s in self.series if s.key == key), None)
        return found or key

    def is_known_series(self, key: str) -> bool:
        return any(s.key == key for s in self.series)

    def source_rank(self, source_key: str) -> str | None:
        src = self.editorial_source(source_key) or self.listing_source(source_key)
        return src.rank if src else None

    def crawlable_listing_sources(self) -> list[ListingSource]:
        """自動クロールを許可されている販売ソースのみを返す。

        mode: manual_only のソース（Zillow / Redfin / Compass）は
        ここから必ず除外される。
        """
        return [s for s in self.listing_sources if s.enabled and s.mode == "crawl"]


def load_config(path: str | Path | None = None) -> Config:
    """config.yaml を読み込んで検証済みの Config を返す。"""
    load_dotenv()
    cfg_path = Path(path or os.environ.get("FREMING_CONFIG", DEFAULT_CONFIG_PATH))
    if not cfg_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {cfg_path.resolve()}")
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """プロセス内で使い回す設定インスタンス。"""
    return load_config()
