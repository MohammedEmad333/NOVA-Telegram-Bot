import com.google.gson.Gson;
import okhttp3.OkHttpClient;
import okhttp3.RequestBody;
import okhttp3.MediaType;
import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.downloader.Downloader;
import org.schabi.newpipe.extractor.downloader.Request;
import org.schabi.newpipe.extractor.downloader.Response;
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException;
import org.schabi.newpipe.extractor.localization.Localization;
import org.schabi.newpipe.extractor.stream.AudioStream;
import org.schabi.newpipe.extractor.stream.DeliveryMethod;
import org.schabi.newpipe.extractor.stream.StreamInfo;
import org.schabi.newpipe.extractor.stream.VideoStream;
import org.schabi.newpipe.extractor.services.youtube.ItagItem;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

public final class NovaExtractor {
    private static final String USER_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0";
    private static final long MAX_BYTES = Long.parseLong(
        System.getenv().getOrDefault("MAX_VIDEO_BYTES", "47185920"));
    private static final Gson GSON = new Gson();

    private static final OkHttpClient HTTP = new OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .followRedirects(true)
        .build();

    private NovaExtractor() {}

    private static final class NovaDownloader extends Downloader {
        @Override
        public Response execute(Request request) throws IOException, ReCaptchaException {
            RequestBody body = request.dataToSend() == null ? null
                : RequestBody.create(request.dataToSend(), (MediaType) null);
            okhttp3.Request.Builder builder = new okhttp3.Request.Builder()
                .url(request.url())
                .method(request.httpMethod(), body)
                .header("User-Agent", USER_AGENT);
            for (Map.Entry<String, List<String>> entry : request.headers().entrySet()) {
                builder.removeHeader(entry.getKey());
                for (String value : entry.getValue()) builder.addHeader(entry.getKey(), value);
            }
            try (okhttp3.Response response = HTTP.newCall(builder.build()).execute()) {
                if (response.code() == 429) {
                    throw new ReCaptchaException("Source rate limited", request.url());
                }
                return new Response(response.code(), response.message(),
                    response.headers().toMultimap(),
                    response.body() == null ? "" : response.body().string(),
                    response.request().url().toString());
            }
        }
    }

    private static long sizeOf(org.schabi.newpipe.extractor.stream.Stream stream) {
        ItagItem itag = stream.getItagItem();
        return itag == null || itag.getContentLength() == ItagItem.CONTENT_LENGTH_UNKNOWN
            ? -1 : itag.getContentLength();
    }

    private static int heightOf(String resolution) {
        if (resolution == null) return 0;
        int end = 0;
        while (end < resolution.length() && Character.isDigit(resolution.charAt(end))) end++;
        if (end == 0) return 0;
        try { return Integer.parseInt(resolution.substring(0, end)); }
        catch (NumberFormatException ignored) { return 0; }
    }

    private static Map<String, Object> streamJson(StreamInfo info, VideoStream video,
                                                   AudioStream audio, boolean muxed) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", true);
        out.put("title", info.getName());
        out.put("uploader", info.getUploaderName());
        out.put("duration_seconds", info.getDuration());
        out.put("height", heightOf(video.getResolution()));
        out.put("resolution", video.getResolution());
        out.put("video_url", video.getContent());
        out.put("audio_url", muxed ? null : audio.getContent());
        out.put("video_bytes", sizeOf(video));
        out.put("audio_bytes", muxed ? 0 : sizeOf(audio));
        out.put("muxed", muxed);
        out.put("extension", video.getFormat() == null ? "mp4" : video.getFormat().getSuffix());
        return out;
    }

    private static void emit(Map<String, Object> value) {
        System.out.println(GSON.toJson(value));
    }

    public static void main(String[] args) {
        if (args.length != 1 || !args[0].startsWith("https://")) {
            System.err.println("Expected one public HTTPS YouTube URL");
            System.exit(2);
        }
        try {
            NewPipe.init(new NovaDownloader(), new Localization("en", "US"));
            StreamInfo info = StreamInfo.getInfo(ServiceList.YouTube, args[0]);

            List<VideoStream> videoOnly = new ArrayList<>();
            for (VideoStream stream : info.getVideoOnlyStreams()) {
                if (stream.getDeliveryMethod() == DeliveryMethod.PROGRESSIVE_HTTP
                    && stream.getFormat() != null
                    && stream.getFormat().getSuffix().equalsIgnoreCase("mp4")
                    && (stream.getCodec() == null || stream.getCodec().toLowerCase().startsWith("avc")
                        || stream.getCodec().toLowerCase().startsWith("hev")
                        || stream.getCodec().toLowerCase().startsWith("hvc"))) {
                    videoOnly.add(stream);
                }
            }
            videoOnly.sort(Comparator.comparingInt((VideoStream v) -> heightOf(v.getResolution())).reversed());

            List<AudioStream> audioStreams = new ArrayList<>();
            for (AudioStream stream : info.getAudioStreams()) {
                if (stream.getDeliveryMethod() == DeliveryMethod.PROGRESSIVE_HTTP
                    && stream.getFormat() != null
                    && stream.getFormat().getSuffix().equalsIgnoreCase("m4a")) {
                    audioStreams.add(stream);
                }
            }
            audioStreams.sort(Comparator.comparingInt(AudioStream::getAverageBitrate).reversed());

            if (!videoOnly.isEmpty() && !audioStreams.isEmpty()) {
                AudioStream audio = audioStreams.get(0);
                long audioSize = sizeOf(audio);
                for (VideoStream video : videoOnly) {
                    long videoSize = sizeOf(video);
                    long total = videoSize < 0 || audioSize < 0 ? -1 : videoSize + audioSize;
                    if (heightOf(video.getResolution()) <= 720
                        && (total < 0 || total <= MAX_BYTES)) {
                        emit(streamJson(info, video, audio, false));
                        return;
                    }
                }
            }

            List<VideoStream> muxed = new ArrayList<>();
            for (VideoStream stream : info.getVideoStreams()) {
                if (stream.getDeliveryMethod() == DeliveryMethod.PROGRESSIVE_HTTP
                    && stream.getFormat() != null
                    && stream.getFormat().getSuffix().equalsIgnoreCase("mp4")) {
                    muxed.add(stream);
                }
            }
            muxed.sort(Comparator.comparingInt((VideoStream v) -> heightOf(v.getResolution())).reversed());
            for (VideoStream video : muxed) {
                long size = sizeOf(video);
                if (heightOf(video.getResolution()) <= 720 && (size < 0 || size <= MAX_BYTES)) {
                    emit(streamJson(info, video, null, true));
                    return;
                }
            }
            Map<String, Object> error = new LinkedHashMap<>();
            error.put("ok", false);
            error.put("reason", "no_compatible_stream_under_limit");
            error.put("message", "No compatible MP4 stream was found within the configured size limit.");
            emit(error);
            System.exit(1);
        } catch (Exception error) {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("ok", false);
            String detail = error.getMessage() == null ? "" : error.getMessage().toLowerCase();
            String reason = detail.contains("captcha") || detail.contains("recaptcha")
                ? "source_challenge" : detail.contains("sign in") || detail.contains("login")
                ? "login_required" : detail.contains("429") ? "source_rate_limited"
                : detail.contains("403") ? "source_forbidden" : "extractor_error";
            result.put("reason", reason);
            result.put("message", "NewPipeExtractor could not resolve this public video.");
            emit(result);
            System.exit(1);
        }
    }
}
