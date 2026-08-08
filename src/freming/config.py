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
    # 代表画像（og:image / 記事の先頭画像）が、物件写真に人物の顔写真を丸く
    # 重ねた合成画像になっているメディア向け。true にすると先頭の1枚を飛ばし、
    # 2枚目から使う。審査UIのサムネイルと納品の 01.jpg の両方に効く。
    skip_lead_image: bool = False
    # フィードから落ちた過去記事を拾う一覧ページ（バックフィル）。
    #
    # 公式RSSはたいてい最新10件しか配信しない。The Spaces の実測では
    # 10件＝7.2日分で、しかも非物件の記事が枠を食うため、物件は6件しか
    # 見えていなかった。カテゴリの一覧ページには20件並んでおり、
    # 11件目から下はフィードに一度も現れない。
    #
    # ここに一覧ページのURLを入れると、フィードの処理のあとに同じページを
    # 見て、まだDBに無い記事を拾う。URLの絞り込みは url_include /
    # url_exclude をそのまま使う（ナビゲーションのリンクを弾くため、
    # 記事URLの形を url_include に書くこと）。
    index_urls: list[str] = Field(default_factory=list)
    # 1回のバックフィルで新しく取り込む上限。一覧ページの件数で頭打ちに
    # なるが、ページ送りを足したときに歯止めが要る。
    max_backfill: int = 20

    def url_allowed(self, url: str) -> bool:
        import re

        if any(re.search(pattern, url) for pattern in self.url_exclude):
            return False
        if not self.url_include:
            return True
        return any(re.search(pattern, url) for pattern in self.url_include)


class ListingCrawl(BaseModel):
    """mode: crawl の販売ソースを、どうたどって何を取るか。

    仲介サイトは記事メディアと違ってRSSを持たないが、公開している
    sitemap.xml に物件ページが並んでいる。そこを入口にする。
    サイトごとに違うのは「物件URLの見分け方」と「どこに価格と所在地が
    書いてあるか」だけなので、コードではなくここで持つ。
    """

    # 入口。sitemap は入れ子になっていることが多いので、物件URLに
    # 行き当たるまで sitemap_depth 段までたどる。
    sitemap_urls: list[str] = Field(default_factory=list)
    # sitemap を持たないサイト用。一覧ページのリンクから拾う。
    index_urls: list[str] = Field(default_factory=list)
    sitemap_depth: int = 2
    # 物件ページのURL。これに一致しないものは取得しない（取得前に落とすので
    # 相手サイトへの無駄なアクセスも減る）。
    detail_url_include: list[str] = Field(default_factory=list)
    detail_url_exclude: list[str] = Field(default_factory=list)
    # 価格の書式。本文から最初に一致したものを売出価格として扱う。
    price_patterns: list[str] = Field(default_factory=lambda: [r"\$\s?\d[\d,]{4,}"])
    # 所在地の取り方。
    #   address    … 表示されている米国住所（"..., Oak Lawn IL 60453"）から取る。
    #                 og:title を先に見て、無ければページ全体から探す。
    #   tw_address … 台湾の住所（"台北市中山區..."）から取る。og:title →
    #                 「地址」の直後 → ページ全体で1種類だけ、の順に見る。
    #   none       … 取らない。スコアリング側の推定に委ねる。
    location_from: Literal["address", "tw_address", "none"] = "address"
    # 所在地を取れなかった物件を候補にしないか。
    #
    # エリアはスコアの2割を占める軸で、空だと採点が成り立たない。
    # `pick_address` は会社の住所を掴むくらいなら None を返す作りなので、
    # 「決められなかったもの」がそのまま所在地不明として入っていた。
    #
    # **`location_from: none` のソースでは常に所在地が取れない**ので、
    # 有効にすると1件も登録されない。国しか分からないサイト（台湾など）を
    # 使うときは、そのソースだけ false にすること。
    require_location: bool = True
    country: str = "United States"
    # 1回の収集で見る物件ページの上限。sitemap は数万件あることがあり、
    # 上限を持たないと3秒間隔でも何時間も走り続ける。
    max_details: int = 40

    def detail_allowed(self, url: str) -> bool:
        import re

        if any(re.search(p, url) for p in self.detail_url_exclude):
            return False
        if not self.detail_url_include:
            return True
        return any(re.search(p, url) for p in self.detail_url_include)


class ListingSource(BaseModel):
    key: str
    name: str
    rank: SourceRank
    mode: ListingMode
    enabled: bool = False
    base_url: str | None = None
    note: str | None = None
    # mode: crawl のときだけ使う。manual_only では未設定のままにする。
    crawl: ListingCrawl | None = None


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
    # **story はゲートであって加点項目ではない。** 承認基準の1・2（前歴が
    # 目に見えるか／様式が特定できるか）は本来 yes/no で、加重平均に混ぜると
    # 他の軸の下駄で埋まってしまう。実際、台湾の仲介物件は story が 0 でも
    # 54点（min_to_persist の1.8倍）に達して審査に上がっていた。
    story_min: float = 40.0
    # これ以降に建てられた物件は落とす。承認基準の第2（時代・様式が特定
    # できること）を数字で裏打ちするもの。築年が読み取れなかったものは
    # 落とさない（不明を落とすと、築年を書いていない良い記事まで消える）。
    # None で無効。
    built_before: int | None = 2000


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
    # effort は対応モデルにだけ渡す。null にすると送らない。
    # 詳細は scoring/client.py の _output_config を見ること。
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
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
    thumbnail_px: int = 250


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


class FxConfig(BaseModel):
    """価格を並べ替えるための円換算レート。

    表示には使わない（審査UIは原文の価格をそのまま出す）。通貨をまたいで
    「高い順・安い順」を出すためだけに使う。

    **DBには換算値を保存しない。** 保存するとレートを直すたびに全件を
    書き直すことになる。並べ替えのときだけ price_value × レート で計算する。
    """

    # 1通貨あたりの円。ここを書き換えれば次の表示から順序に反映される。
    jpy_per: dict[str, float] = Field(default_factory=dict)
    # レートの基準日。いつの値かが分からないと、直すべきかの判断ができない。
    as_of: str = ""

    def rate(self, currency: str | None) -> float | None:
        return self.jpy_per.get(currency) if currency else None


class ImagesConfig(BaseModel):
    max_per_property: int = 10
    min_short_edge_px: int = 640
    # 代表画像が単色（＝サイト側の「写真なし」プレースホルダ）の物件を
    # 収集時に落とす。寸法は本物と同じことが多く min_short_edge_px では
    # 落ちないため、別の条件として持つ。
    require_real_photo: bool = True
    flat_stddev_max: float = 3.0
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


class HashtagRule(BaseModel):
    """物件の内容に応じて足すタグ。match は部分一致（小文字で比較）。"""

    match: str
    tags: list[str] = Field(default_factory=list)


class CaptionConfig(BaseModel):
    """[9] 投稿の本文。実際の @frmg.jpn の投稿から起こした型（2026-08-07）。

    **文言はここだけで直す。** 組み立ての順は instagram/caption.py が持つ。
    価格は入れない（実物にも入っていない。通貨が混ざるうえ為替で見え方が
    変わり、成約後も直せない）。
    """

    lead: list[str] = Field(default_factory=list)
    # 出力する順とラベル。key は properties の列名（location だけ特別扱い）。
    spec: list[tuple[str, str]] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    disclaimer: str = ""
    photo_credit_label: str = "Photo"
    photo_credit_fallback_source: bool = True
    signature: list[str] = Field(default_factory=list)
    business: str = ""
    hashtags: list[str] = Field(default_factory=list)
    hashtag_rules: list[HashtagRule] = Field(default_factory=list)


class ReelConfig(BaseModel):
    """[9] 週次リール。既定値は実物を見て決めたもので、勝手な仮置きではない。

    size / square_offset_px / zoom は 2026-08-07 に3案を書き出して
    見比べたうえで確定した。変えるときも同じように書き出して見ること。
    """

    size: tuple[int, int] = (1080, 1920)
    image_count: int = 7          # 1日3投稿から各日の1位を7日ぶん
    total_sec: float = 21.0
    crossfade_sec: float = 0.4
    zoom: float = 0.10            # 1枚の尺で 1.10 倍まで寄る。0 で寄らない
    square_offset_px: int = 60    # 正方形を中央より上げる量。Reels の下UIを避ける
    blur_radius: int = 60         # 背景のぼかし
    bg_brightness: float = 0.55   # 背景を暗くする係数。前景の正方形を立たせる
    jpeg_quality: int = 92
    fps: int = 30
    crf: int = 20
    x264_preset: str = "medium"
    audio_bitrate: str = "128k"
    audio_fade_in_sec: float = 0.5
    audio_fade_out_sec: float = 1.5


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
    #
    # **環境変数 FREMING_DRIVE_AUTH_MODE で上書きできる。** 移行期に
    # 「手元は oauth のまま / GitHub Actions だけ adc」を両立させるため。
    # config.yaml を書き換えると片方が必ず壊れる。
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


class InstagramConfig(BaseModel):
    """Instagram 自動投稿（[8][9]）。トークンは DB（api_tokens）に持つ。

    app_id は公開値なのでここに置く。app_secret は OAuth コールバック方式を
    使うときだけ必要で、環境変数 INSTAGRAM_APP_SECRET からのみ読む。
    """

    app_id: str | None = None

    # --- 投稿 ---------------------------------------------------------
    # auto_post は**1箇所でだけ true にする**。複数の環境で同時に立てると
    # 同じ予定を取り合う。posts の状態遷移で二重投稿は防いでいるが、
    # そもそも1箇所に寄せるのが前提（納品ワーカーと同じ方針）。
    auto_post: bool = False
    # このプロセスが担当する種別。**動かす場所を分けるために使う。**
    #
    # リール（reel）は ffmpeg と数百MBのメモリが要るので、審査UI
    # （Render の無料プラン）では作れない。通常投稿は画像を配る側と
    # 同じ場所に置きたい。そこで場所ごとに担当を書き分ける。
    #
    # 暗黙に分けない。設定に書いていないと、片方が止まったときに
    # 誰も気づけない（auto_post を1箇所だけにするのと同じ理由）。
    worker_kinds: list[str] = Field(default_factory=lambda: ["feed", "story"])
    # Meta が画像を取りに来る先。審査UIの公開URL。末尾のスラッシュは不要。
    # **これが空だと投稿できない。** Meta はローカルのパスを読めない。
    public_base_url: str | None = None
    poll_interval_sec: float = 60.0
    max_attempts: int = 3
    # 投稿時刻（JST の HH:MM）。並びがそのまま1日の投稿順になる。
    post_times: list[str] = Field(default_factory=lambda: ["09:00", "13:00", "20:00"])
    timezone: str = "Asia/Tokyo"
    # 予定を何日先まで作るか。審査UIの予定表もこの日数を出す。
    plan_days: int = 3
    # 投稿のあと何分でストーリーズを出すか。0 で同時。
    story_delay_min: int = 5
    post_story: bool = True
    # 週次リール。weekday は 0=月曜。8日目に出すので、収集の週と1日ずらす。
    post_reel: bool = True
    reel_weekday: int = 0
    reel_time: str = "19:00"
    # リールに使う画像を選ぶのに必要なリーチが取れないとき、
    # 直近の投稿で代用してよいか。既定は **代用しない**（黙って別物を出さない）。
    reel_fallback_recent: bool = False
    # 自動投稿してよいソース。空なら「manual_only でない全ソース」。
    # 仲介サイト由来の写真は MLS のロゴが載ることがあるので、
    # 自動で出す先はここで絞れるようにしてある。
    allowed_sources: list[str] = Field(default_factory=list)

    def public_media_url(self, token: str) -> str:
        base = (self.public_base_url or "").rstrip("/")
        if not base:
            raise ValueError(
                "instagram.public_base_url が未設定です。"
                "Meta はこちらのサーバーへ画像を取りに来るので、"
                "審査UIの公開URLを config.yaml に設定してください。"
            )
        return f"{base}/m/{token}"


def _drive_auth_override(config: DriveConfig) -> DriveConfig:
    """FREMING_DRIVE_AUTH_MODE があればそれを使う。

    納品を GitHub Actions（Workload Identity 連携）へ移す途中、手元の
    oauth と両立させるための逃げ道。config.yaml を書き換えると
    どちらか片方が必ず壊れる。
    """
    mode = os.environ.get("FREMING_DRIVE_AUTH_MODE", "").strip()
    if mode and mode != config.auth_mode:
        if mode not in ("oauth", "service_account", "adc"):
            raise ValueError(f"FREMING_DRIVE_AUTH_MODE が不正です: {mode}")
        return config.model_copy(update={"auth_mode": mode})
    return config


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
    fx: FxConfig = Field(default_factory=FxConfig)
    process: ProcessConfig = Field(default_factory=ProcessConfig)
    caption: CaptionConfig = Field(default_factory=CaptionConfig)
    reel: ReelConfig = Field(default_factory=ReelConfig)
    drive: DriveConfig
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)

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
    config = Config.model_validate(raw)
    config.drive = _drive_auth_override(config.drive)
    return config


@lru_cache(maxsize=1)
def get_config() -> Config:
    """プロセス内で使い回す設定インスタンス。"""
    return load_config()
