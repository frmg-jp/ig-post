// @frmg.jpn の担当者に送る「再認可のお願い」スライド。
//
// 前回（トークン発行）とは別の作業。権限を1つ足すために、もう一度だけ
// 認可を押してもらう。作業は1分で終わる。
//
// 配色は frmg.jp のトークンに合わせる（オフホワイトの地・墨の文字・
// 赤は1点だけ）。角丸は使わない。
const pptxgen = require("pptxgenjs");

const BG = "EFEFEF";
const INK = "1C1B1A";
const FILL = "DCDCDC";
const HI = "FF3500";
const WHITE = "FFFFFF";

// 和文が入るので日本語フォントを指定する。IPAGothic は変換に使う
// LibreOffice にも入っているので、書き出したPDFと本番の見え方がずれない。
const JP = "IPAGothic";

const W = 13.333;
const H = 7.5;
const M = 0.9;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "FREMING CURATED";
pres.title = "Instagram 連携の再認可のお願い";

function label(slide, text) {
  slide.addText(text, {
    x: M, y: 0.5, w: W - M * 2, h: 0.3,
    fontFace: JP, fontSize: 11, color: INK, charSpacing: 2,
    margin: 0,
  });
}

function stepNumber(slide, n) {
  slide.addShape(pres.ShapeType.rect, {
    x: M, y: 1.05, w: 0.62, h: 0.62, fill: { color: INK }, line: { color: INK },
  });
  slide.addText(String(n), {
    x: M, y: 1.05, w: 0.62, h: 0.62,
    fontFace: "Arial", fontSize: 26, color: WHITE, align: "center", valign: "middle",
    margin: 0,
  });
}

// STEP 番号がある回だけ字下げする。番号の無い回で下げると、
// 見出しだけが宙に浮いて位置が揃わない。
function heading(slide, text, { stepped = true, y = 1.05 } = {}) {
  const x = stepped ? M + 0.95 : M;
  slide.addText(text, {
    x, y, w: W - M * 2 - (stepped ? 0.95 : 0), h: 0.75,
    fontFace: JP, fontSize: 27, color: INK, valign: "middle", margin: 0,
  });
}

function rule(slide, y) {
  slide.addShape(pres.ShapeType.line, {
    x: M, y, w: W - M * 2, h: 0, line: { color: INK, width: 1 },
  });
}

function newSlide() {
  const s = pres.addSlide();
  s.background = { color: BG };
  return s;
}

// ---------------------------------------------------------------- 表紙
{
  const s = newSlide();
  s.background = { color: INK };
  s.addText("FREMING CURATED", {
    x: M, y: 1.5, w: W - M * 2, h: 0.4,
    fontFace: JP, fontSize: 12, color: BG, charSpacing: 3, margin: 0,
  });
  s.addText("Instagram 連携の\n再認可のお願い", {
    x: M, y: 2.1, w: W - M * 2, h: 2.0,
    fontFace: JP, fontSize: 42, color: BG, lineSpacing: 52, margin: 0,
  });
  s.addShape(pres.ShapeType.line, {
    x: M, y: 4.5, w: 3.2, h: 0, line: { color: HI, width: 3 },
  });
  s.addText("お願いすること … リンクを開いて「許可」を押す\n所要時間 … 1分\n必要なもの … PCのブラウザ（@frmg.jpn でログイン中のもの）", {
    x: M, y: 4.9, w: W - M * 2, h: 1.4,
    fontFace: JP, fontSize: 15, color: BG, lineSpacing: 30, margin: 0,
  });
  s.addNotes("前回のトークン発行とは別の作業です。権限を1つ足すために、もう一度だけ認可が必要になりました。");
}

// ------------------------------------------------------- なぜ必要か
{
  const s = newSlide();
  label(s, "はじめに");
  heading(s, "なぜ、もう一度お願いするのか", { stepped: false });
  rule(s, 2.05);

  s.addText("前回いただいた許可では、投稿はできますが\n「その投稿がどれだけ見られたか」を読む権限がありません。", {
    x: M, y: 2.35, w: W - M * 2, h: 0.9,
    fontFace: JP, fontSize: 18, color: INK, lineSpacing: 32, margin: 0,
  });

  s.addShape(pres.ShapeType.rect, {
    x: M, y: 3.45, w: W - M * 2, h: 1.5, fill: { color: FILL }, line: { color: FILL },
  });
  s.addText(
    "週に1回、その週で最も見られた投稿を集めた動画を出す予定です。\n" +
    "「最も見られた」を判定するのに、この権限が要ります。",
    {
      x: M + 0.35, y: 3.45, w: W - M * 2 - 0.7, h: 1.5,
      fontFace: JP, fontSize: 16, color: INK, lineSpacing: 30, valign: "middle", margin: 0,
    }
  );

  s.addText("権限はあとから足せない決まりのため、認可からやり直しになります。\n前回のトークンは無効になりません。今回のぶんが上書きされるだけです。", {
    x: M, y: 5.25, w: W - M * 2, h: 1.0,
    fontFace: JP, fontSize: 14, color: INK, lineSpacing: 28, margin: 0,
  });
  s.addNotes("スコープ（権限）は refresh では増やせないので、OAuth の認可からやり直す必要があります。");
}

// --------------------------------------------------------- STEP 1
{
  const s = newSlide();
  label(s, "STEP 1");
  stepNumber(s, 1);
  heading(s, "送られてきたリンクを開く");
  rule(s, 2.05);

  s.addText("メールまたはチャットで届いた URL を、\nそのままブラウザのアドレス欄に貼って開いてください。", {
    x: M, y: 2.35, w: W - M * 2, h: 0.9,
    fontFace: JP, fontSize: 18, color: INK, lineSpacing: 32, margin: 0,
  });

  s.addShape(pres.ShapeType.rect, {
    x: M, y: 3.5, w: W - M * 2, h: 2.35, fill: { color: FILL }, line: { color: FILL },
  });
  s.addText("ここだけ注意してください", {
    x: M + 0.35, y: 3.7, w: W - M * 2 - 0.7, h: 0.4,
    fontFace: JP, fontSize: 14, color: INK, charSpacing: 1, margin: 0,
  });
  s.addText(
    [
      { text: "PCのブラウザで開いてください（スマホのアプリではありません）", options: { bullet: true, breakLine: true } },
      { text: "そのブラウザで @frmg.jpn にログインしている必要があります", options: { bullet: true, breakLine: true } },
      { text: "別のアカウントでログインしていると、権限が足りないと表示されます", options: { bullet: true } },
    ],
    {
      x: M + 0.5, y: 4.15, w: W - M * 2 - 1.0, h: 1.5,
      fontFace: JP, fontSize: 15, color: INK, paraSpaceAfter: 10, margin: 0,
    }
  );
  s.addNotes("Insufficient Developer Role は、ブラウザのログインアカウントが違うときに出ます。");
}

// --------------------------------------------------------- STEP 2
{
  const s = newSlide();
  label(s, "STEP 2");
  stepNumber(s, 2);
  heading(s, "「許可」を押す");
  rule(s, 2.05);

  s.addText("FREMING のアプリが求める権限の一覧が出ます。\n内容を確認して、そのまま「許可」を押してください。", {
    x: M, y: 2.35, w: W - M * 2, h: 0.9,
    fontFace: JP, fontSize: 18, color: INK, lineSpacing: 32, margin: 0,
  });

  const perms = [
    ["アカウント情報の読み取り", "ユーザー名の確認に使います"],
    ["コンテンツの投稿", "物件の投稿とリールに使います"],
    ["インサイトの読み取り", "今回追加するものです"],
  ];
  perms.forEach(([name, why], i) => {
    const y = 3.55 + i * 0.85;
    s.addShape(pres.ShapeType.rect, {
      x: M, y, w: W - M * 2, h: 0.68,
      fill: { color: i === 2 ? INK : FILL }, line: { color: i === 2 ? INK : FILL },
    });
    s.addText(name, {
      x: M + 0.35, y, w: 4.4, h: 0.68,
      fontFace: JP, fontSize: 16, color: i === 2 ? BG : INK, valign: "middle", margin: 0,
    });
    s.addText(why, {
      x: M + 4.9, y, w: W - M * 2 - 5.25, h: 0.68,
      fontFace: JP, fontSize: 14, color: i === 2 ? BG : INK, valign: "middle", margin: 0,
    });
  });

  s.addText("メッセージやコメントを扱う権限は求めていません。", {
    x: M, y: 6.2, w: W - M * 2, h: 0.4,
    fontFace: JP, fontSize: 13, color: INK, margin: 0,
  });
}

// --------------------------------------------------------- STEP 3
{
  const s = newSlide();
  label(s, "STEP 3");
  stepNumber(s, 3);
  heading(s, "画面に出た文字列を送り返す");
  rule(s, 2.05);

  s.addText("「許可」を押すと FREMING の画面に移り、長い文字列が表示されます。\nそれをコピーして、そのまま返信してください。これで完了です。", {
    x: M, y: 2.35, w: W - M * 2, h: 0.9,
    fontFace: JP, fontSize: 18, color: INK, lineSpacing: 32, margin: 0,
  });

  s.addShape(pres.ShapeType.rect, {
    x: M, y: 3.5, w: W - M * 2, h: 1.05, fill: { color: WHITE }, line: { color: INK, width: 1 },
  });
  s.addText("AQK4wiCOOxi1hjO2KR-mqws...（実際はもっと長い文字列です）", {
    x: M + 0.35, y: 3.5, w: W - M * 2 - 0.7, h: 1.05,
    fontFace: "Courier New", fontSize: 15, color: INK, valign: "middle", margin: 0,
  });

  s.addText(
    [
      { text: "URL の一部ではなく、画面に出ている文字列そのものです", options: { bullet: true, breakLine: true } },
      { text: "末尾に # が付いていたら、それも含めて構いません", options: { bullet: true, breakLine: true } },
      { text: "この文字列は数分で使えなくなります。届いたらすぐ送ってください", options: { bullet: true } },
    ],
    {
      x: M + 0.15, y: 4.85, w: W - M * 2 - 0.3, h: 1.5,
      fontFace: JP, fontSize: 15, color: INK, paraSpaceAfter: 10, margin: 0,
    }
  );
  s.addNotes("code は一度きり・短時間で失効します。画面に出しても後から使い回せません。");
}

// ------------------------------------------------- うまくいかないとき
{
  const s = newSlide();
  label(s, "うまくいかないとき");
  heading(s, "よくある3つ", { stepped: false });
  rule(s, 2.05);

  const cases = [
    ["Insufficient Developer Role と出る",
     "ブラウザが別のアカウントでログインしています。@frmg.jpn でログインし直すか、\nシークレットウィンドウで開いてログインしてください。"],
    ["招待の承認を求められる",
     "前回の作業で承認いただいているはずですが、もし再度求められた場合は\nこの資料を送った担当までご連絡ください。招待を送り直します。"],
    ["白い画面のまま何も出ない",
     "サーバーが起動中の可能性があります。1分ほど待ってから読み込み直してください。"],
  ];
  cases.forEach(([q, a], i) => {
    const y = 2.35 + i * 1.55;
    s.addText(q, {
      x: M, y, w: W - M * 2, h: 0.4,
      fontFace: JP, fontSize: 17, color: HI, margin: 0,
    });
    s.addText(a, {
      x: M, y: y + 0.42, w: W - M * 2, h: 0.9,
      fontFace: JP, fontSize: 14, color: INK, lineSpacing: 26, margin: 0,
    });
  });
}

// ---------------------------------------------------------- まとめ
{
  const s = newSlide();
  s.background = { color: INK };
  s.addText("まとめ", {
    x: M, y: 1.3, w: W - M * 2, h: 0.4,
    fontFace: JP, fontSize: 12, color: BG, charSpacing: 3, margin: 0,
  });
  s.addText("お願いするのは、この3つだけです", {
    x: M, y: 1.9, w: W - M * 2, h: 0.7,
    fontFace: JP, fontSize: 30, color: BG, margin: 0,
  });
  s.addShape(pres.ShapeType.line, {
    x: M, y: 2.95, w: 3.2, h: 0, line: { color: HI, width: 3 },
  });
  s.addText(
    "1.　送られてきたリンクを PC のブラウザで開く\n" +
    "2.　「許可」を押す\n" +
    "3.　表示された文字列をコピーして返信する",
    {
      x: M, y: 3.4, w: W - M * 2, h: 2.0,
      fontFace: JP, fontSize: 20, color: BG, lineSpacing: 46, margin: 0,
    }
  );
  s.addText("ご不明な点があれば、この資料を送った担当までご連絡ください。", {
    x: M, y: 5.9, w: W - M * 2, h: 0.4,
    fontFace: JP, fontSize: 13, color: BG, margin: 0,
  });
}

pres.writeFile({ fileName: "frmg-reauth.pptx" }).then(() => console.log("書き出しました: frmg-reauth.pptx"));
