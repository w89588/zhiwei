"""Bounded conversions for user-provided media; contents are untrusted evidence."""
import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_TEXT = 200000
MAX_AUDIO_WAV = 24 * 1024 * 1024
# The provider limits the Base64 representation of audio/video to 10 MB.
MAX_MODEL_MEDIA = 7 * 1024 * 1024


def silk_to_wav(data):
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('语音文件过大，暂无法转换')
    program = r'''import io,sys,wave,pysilk
class Bounded(io.BytesIO):
 def write(self, data):
  if self.tell()+len(data)>24*1024*1024: raise ValueError('audio too long')
  return super().write(data)
source=io.BytesIO(sys.stdin.buffer.read()); pcm=Bounded(); pysilk.decode(source,pcm,24000)
out=io.BytesIO()
with wave.open(out,'wb') as wav:
 wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(pcm.getvalue())
sys.stdout.buffer.write(out.getvalue())
'''
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        result = subprocess.run([sys.executable, '-c', program], input=data, capture_output=True,
                                timeout=30, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('语音转换失败或超时') from None
    if result.returncode or len(result.stdout) > MAX_AUDIO_WAV or len(result.stdout) <= 44:
        raise ValueError('语音格式无法转换')
    return result.stdout


def document_text(name, data):
    suffix = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if suffix in {'txt', 'md', 'csv', 'tsv', 'json', 'xml', 'log'}:
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('文本文件过大，暂无法分析')
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError('文本文件编码不支持') from None
    elif suffix == 'pdf':
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('PDF 文件过大，暂无法分析')
        from pypdf import PdfReader
        try:
            pdf = PdfReader(io.BytesIO(data), strict=False)
            if pdf.is_encrypted or len(pdf.pages) > 100:
                raise ValueError('PDF 已加密或超过 100 页')
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        except Exception:
            raise ValueError('PDF 正文无法提取') from None
    elif suffix in {'docx', 'pptx', 'xlsx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if suffix == 'docx':
                    names = [n for n in archive.namelist() if n == 'word/document.xml']
                elif suffix == 'pptx':
                    names = [n for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')]
                else:
                    names = [n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
                    if 'xl/sharedStrings.xml' in archive.namelist():
                        names.append('xl/sharedStrings.xml')
                if not names or sum(archive.getinfo(n).file_size for n in names) > 20 * 1024 * 1024:
                    raise ValueError('document too large')
                pieces = []
                shared = []
                if suffix == 'xlsx' and 'xl/sharedStrings.xml' in names:
                    strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
                    shared = [''.join(n.text or '' for n in entry.iter() if n.tag.endswith('}t')) for entry in strings]
                for entry in sorted(n for n in names if n != 'xl/sharedStrings.xml'):
                    xml = ET.fromstring(archive.read(entry))
                    if suffix == 'xlsx':
                        for row in (n for n in xml.iter() if n.tag.endswith('}row')):
                            cells = []
                            for cell in row:
                                if not cell.tag.endswith('}c'):
                                    continue
                                value = next((n.text for n in cell.iter() if n.tag.endswith('}v') and n.text), '')
                                if cell.get('t') == 's':
                                    index = int(value)
                                    if index < 0:
                                        raise ValueError('invalid shared string')
                                    value = shared[index]
                                elif cell.get('t') == 'inlineStr':
                                    value = ''.join(n.text or '' for n in cell.iter() if n.tag.endswith('}t'))
                                cells.append(value)
                            pieces.append('\t'.join(cells))
                    else:
                        pieces.extend(node.text for node in xml.iter() if node.tag.endswith('}t') and node.text)
                text = '\n'.join(pieces)
        except (ValueError, IndexError, KeyError, zipfile.BadZipFile, ET.ParseError):
            raise ValueError('文档正文无法提取') from None
    else:
        raise ValueError('该文件格式暂不支持正文提取')
    if not text.strip():
        raise ValueError('文件没有可提取的文字')
    return text[:MAX_TEXT] + ('\n[文件正文超过长度上限，以下内容未提取]' if len(text) > MAX_TEXT else '')


def encoded_data(data, mime):
    import base64
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


def audio_for_model(wav):
    if len(wav) <= MAX_MODEL_MEDIA:
        return wav, 'wav'
    import imageio_ffmpeg
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error',
            '-f', 'wav', '-i', 'pipe:0', '-c:a', 'libmp3lame', '-b:a', '48k', '-f', 'mp3', 'pipe:1'],
            input=wav, capture_output=True, timeout=60, creationflags=flags)
        if result.returncode or not result.stdout or len(result.stdout) > MAX_MODEL_MEDIA:
            raise ValueError('语音压缩失败或超过模型输入限制')
        return result.stdout, 'mp3'
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('语音压缩失败或超时') from None


def video_for_model(data):
    if len(data) <= MAX_MODEL_MEDIA:
        return data, False
    import imageio_ffmpeg
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        with tempfile.TemporaryDirectory(prefix='wechat-video-') as folder:
            source, target = Path(folder)/'source.mp4', Path(folder)/'model.mp4'
            source.write_bytes(data)
            command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-vf', 'scale=640:-2',
                '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '192k', '-c:a', 'aac', '-b:a', '48k',
                '-movflags', '+faststart', '-y', str(target)]
            result = subprocess.run(command, capture_output=True, timeout=120, creationflags=flags)
            if result.returncode or not target.is_file() or target.stat().st_size > MAX_MODEL_MEDIA:
                raise ValueError('视频压缩失败或仍超过模型输入限制')
            return target.read_bytes(), True
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('视频压缩失败或超时') from None
