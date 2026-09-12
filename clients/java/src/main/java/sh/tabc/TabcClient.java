package sh.tabc;

import java.io.IOException;
import java.math.BigInteger;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.EdECPrivateKeySpec;
import java.security.spec.NamedParameterSpec;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutionException;

/**
 * Small Java client for sending signed messages to tabd.
 *
 * <p>The private node key is the same raw 32-byte Ed25519 key created by {@code tabc register}.
 * It is read locally and never sent. The exact HTTP body is signed with the same canonical request
 * contract as the Python CLI.
 */
public final class TabcClient {

    private static final String SEND_PATH = "/send";
    private static final String BASE58 =
            "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
    private static final Set<String> PRIORITIES = Set.of("now", "next", "batch");

    private final URI sendUri;
    private final String senderNode;
    private final PrivateKey privateKey;
    private final Duration requestTimeout;
    private final HttpClient httpClient;

    public TabcClient(
            URI baseUri,
            String senderNode,
            Path privateKeyPath,
            Duration connectTimeout,
            Duration requestTimeout) {
        Objects.requireNonNull(baseUri, "baseUri");
        this.senderNode = requireSafeNodeId(senderNode);
        this.privateKey = loadPrivateKey(Objects.requireNonNull(privateKeyPath, "privateKeyPath"));
        this.requestTimeout = requirePositive(requestTimeout, "requestTimeout");
        this.httpClient =
                HttpClient.newBuilder()
                        .connectTimeout(requirePositive(connectTimeout, "connectTimeout"))
                        .build();
        this.sendUri = baseUri.resolve(SEND_PATH);
        String scheme = sendUri.getScheme();
        if (!("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))) {
            throw new IllegalArgumentException("tabd base URI must use http or https");
        }
    }

    /** Send one durable envelope. A configurable event prefix belongs in {@code subject}. */
    public SendResult send(
            List<String> recipients, String subject, String body, String priority) {
        return send(recipients, subject, body, priority, UUID.randomUUID().toString());
    }

    /**
     * Send one durable envelope with a caller-minted idempotency key. Reusing the same id with an
     * identical envelope is safe; callers must not retry an UNKNOWN result with a new id.
     */
    public SendResult send(
            List<String> recipients,
            String subject,
            String body,
            String priority,
            String messageId) {
        try {
            return sendAsync(recipients, subject, body, priority, messageId).get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return new SendResult(Outcome.UNKNOWN, -1, describe(e));
        } catch (Exception e) {
            return new SendResult(Outcome.UNKNOWN, -1, describe(rootCause(e)));
        }
    }

    /** Send one durable envelope without blocking the calling thread. */
    public CompletableFuture<SendResult> sendAsync(
            List<String> recipients, String subject, String body, String priority) {
        return sendAsync(recipients, subject, body, priority, UUID.randomUUID().toString());
    }

    /**
     * Send one durable envelope asynchronously with a caller-minted idempotency key. No automatic
     * retry is performed, and an UNKNOWN result must be retried only with the same message id and
     * identical envelope.
     */
    public CompletableFuture<SendResult> sendAsync(
            List<String> recipients,
            String subject,
            String body,
            String priority,
            String messageId) {
        List<String> checkedRecipients = requireRecipients(recipients);
        Objects.requireNonNull(subject, "subject");
        Objects.requireNonNull(body, "body");
        String checkedMessageId = requireMessageId(messageId);
        if (!PRIORITIES.contains(priority)) {
            throw new IllegalArgumentException("priority must be now, next, or batch");
        }

        String requestBody =
                "{\"from\":"
                        + jsonString(senderNode)
                        + ",\"to\":"
                        + jsonArray(checkedRecipients)
                        + ",\"subject\":"
                        + jsonString(subject)
                        + ",\"body\":"
                        + jsonString(body)
                        + ",\"priority\":"
                        + jsonString(priority)
                        + ",\"message_id\":"
                        + jsonString(checkedMessageId)
                        + "}";
        String timestamp = Long.toString(Instant.now().getEpochSecond());

        final HttpRequest request;
        try {
            String signature =
                    base58Encode(
                            sign(
                                    canonicalRequest(
                                            senderNode,
                                            "POST",
                                            SEND_PATH,
                                            requestBody,
                                            timestamp)));
            request =
                    HttpRequest.newBuilder(sendUri)
                            .timeout(requestTimeout)
                            .header("Content-Type", "application/json; charset=utf-8")
                            .header("X-Node", senderNode)
                            .header("X-Node-Ts", timestamp)
                            .header("X-Node-Sig", signature)
                            .POST(HttpRequest.BodyPublishers.ofString(requestBody, StandardCharsets.UTF_8))
                            .build();
        } catch (Exception e) {
            return CompletableFuture.completedFuture(
                    new SendResult(Outcome.UNKNOWN, -1, describe(e)));
        }

        return httpClient
                .sendAsync(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8))
                .handle(
                        (response, failure) -> {
                            if (failure != null) {
                                return new SendResult(
                                        Outcome.UNKNOWN,
                                        -1,
                                        describe(rootCause(failure)));
                            }
                            return mapResponse(response);
                        });
    }

    public enum Outcome {
        STORED,
        FAILED,
        UNKNOWN
    }

    public record SendResult(Outcome outcome, int httpStatus, String responseBody) {
        public SendResult {
            Objects.requireNonNull(outcome, "outcome");
            responseBody = responseBody == null ? "" : responseBody;
        }
    }

    private byte[] sign(byte[] canonical) throws Exception {
        Signature signer = Signature.getInstance("Ed25519");
        signer.initSign(privateKey);
        signer.update(canonical);
        return signer.sign();
    }

    private static PrivateKey loadPrivateKey(Path path) {
        try {
            requirePrivatePermissions(path);
            byte[] raw = Files.readAllBytes(path);
            if (raw.length != 32) {
                throw new IllegalArgumentException(
                        "node key must be exactly 32 bytes: " + path + " (got " + raw.length + ")");
            }
            KeyFactory factory = KeyFactory.getInstance("Ed25519");
            return factory.generatePrivate(
                    new EdECPrivateKeySpec(NamedParameterSpec.ED25519, raw));
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalArgumentException("cannot load node key: " + path, e);
        }
    }

    private static void requirePrivatePermissions(Path path) throws IOException {
        try {
            Set<PosixFilePermission> permissions = Files.getPosixFilePermissions(path);
            boolean exposed =
                    permissions.contains(PosixFilePermission.GROUP_READ)
                            || permissions.contains(PosixFilePermission.GROUP_WRITE)
                            || permissions.contains(PosixFilePermission.GROUP_EXECUTE)
                            || permissions.contains(PosixFilePermission.OTHERS_READ)
                            || permissions.contains(PosixFilePermission.OTHERS_WRITE)
                            || permissions.contains(PosixFilePermission.OTHERS_EXECUTE);
            if (exposed) {
                throw new IllegalArgumentException(
                        "node key must not be accessible by group or others: " + path);
            }
        } catch (UnsupportedOperationException ignored) {
            // Non-POSIX file systems have no equivalent permission bits to inspect here.
        }
    }

    private static byte[] canonicalRequest(
            String node, String method, String path, String body, String timestamp)
            throws Exception {
        String canonical =
                String.join(
                        "\n",
                        sha256(node),
                        sha256(method),
                        sha256(path),
                        sha256(body),
                        sha256(timestamp));
        return canonical.getBytes(StandardCharsets.UTF_8);
    }

    private static String sha256(String value) throws Exception {
        return HexFormat.of()
                .formatHex(
                        MessageDigest.getInstance("SHA-256")
                                .digest(value.getBytes(StandardCharsets.UTF_8)));
    }

    private static String base58Encode(byte[] bytes) {
        BigInteger value = new BigInteger(1, bytes);
        StringBuilder encoded = new StringBuilder();
        BigInteger radix = BigInteger.valueOf(58);
        while (value.signum() > 0) {
            BigInteger[] divRem = value.divideAndRemainder(radix);
            encoded.append(BASE58.charAt(divRem[1].intValue()));
            value = divRem[0];
        }
        for (byte b : bytes) {
            if (b != 0) break;
            encoded.append('1');
        }
        return encoded.reverse().toString();
    }

    private static String jsonArray(List<String> values) {
        StringBuilder out = new StringBuilder("[");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) out.append(',');
            out.append(jsonString(values.get(i)));
        }
        return out.append(']').toString();
    }

    private static String jsonString(String value) {
        StringBuilder out = new StringBuilder(value.length() + 2).append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        return out.append('"').toString();
    }

    private static List<String> requireRecipients(List<String> recipients) {
        if (recipients == null || recipients.isEmpty()) {
            throw new IllegalArgumentException("at least one recipient is required");
        }
        return recipients.stream().map(TabcClient::requireSafeNodeId).distinct().toList();
    }

    private static String requireMessageId(String messageId) {
        if (messageId == null || messageId.isBlank() || messageId.length() > 256) {
            throw new IllegalArgumentException("message id must contain 1 to 256 characters");
        }
        return messageId;
    }

    private static String requireSafeNodeId(String nodeId) {
        if (nodeId == null || nodeId.isBlank()) {
            throw new IllegalArgumentException("node id is required");
        }
        for (int i = 0; i < nodeId.length(); i++) {
            char c = nodeId.charAt(i);
            boolean safe =
                    (c >= 'a' && c <= 'z')
                            || (c >= 'A' && c <= 'Z')
                            || (c >= '0' && c <= '9')
                            || c == '-'
                            || c == '_';
            if (!safe) {
                throw new IllegalArgumentException(
                        "node id may contain only ASCII letters, digits, dash, and underscore");
            }
        }
        return nodeId;
    }

    private static Duration requirePositive(Duration duration, String name) {
        if (duration == null || duration.isZero() || duration.isNegative()) {
            throw new IllegalArgumentException(name + " must be positive");
        }
        return duration;
    }

    private static String describe(Exception e) {
        String message = e.getMessage();
        return e.getClass().getSimpleName() + (message == null ? "" : ": " + message);
    }

    private static SendResult mapResponse(HttpResponse<String> response) {
        int status = response.statusCode();
        if (status == 200) {
            return new SendResult(Outcome.STORED, status, response.body());
        }
        if (status >= 400 && status < 500) {
            return new SendResult(Outcome.FAILED, status, response.body());
        }
        return new SendResult(Outcome.UNKNOWN, status, response.body());
    }

    private static Exception rootCause(Throwable failure) {
        Throwable current = failure;
        while ((current instanceof CompletionException || current instanceof ExecutionException)
                && current.getCause() != null) {
            current = current.getCause();
        }
        if (current instanceof Exception exception) {
            return exception;
        }
        return new Exception(current);
    }
}
