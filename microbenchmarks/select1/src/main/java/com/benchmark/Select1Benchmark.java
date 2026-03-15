package com.benchmark;

import com.netflix.concurrency.limits.Limit;
import com.netflix.concurrency.limits.Limiter;
import com.netflix.concurrency.limits.limit.AIMDLimit;
import com.netflix.concurrency.limits.limit.GradientLimit;
import com.netflix.concurrency.limits.limit.VegasLimit;
import com.netflix.concurrency.limits.limiter.BlockingLimiter;
import com.netflix.concurrency.limits.limiter.SimpleLimiter;
import org.apache.commons.cli.*;
import org.apache.commons.math3.stat.descriptive.rank.Percentile;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

public class Select1Benchmark {
    private static final String DEFAULT_PORT = "5432";
    private static final int DEFAULT_MIN_INFLIGHT = 1;
    private static final int DEFAULT_MAX_INFLIGHT = 64;
    private static final int DEFAULT_INTERVAL_SECONDS = 5;
    private static final String DEFAULT_FORMAT = "human";
    private static final int DEFAULT_LIMITER_INITIAL_LIMIT = 16;
    private static final String DEFAULT_LIMITER_NAME = "select1-query";
    private static final String DEFAULT_LIMITER_ALGORITHM = "vegas";

    private enum OutputFormat {
        HUMAN,
        CSV
    }

    private enum LimiterAlgorithm {
        VEGAS,
        GRADIENT,
        AIMD
    }

    private static class BenchmarkResult {
        final int inflight;
        final double rps;
        final double pureP50;
        final double pureP90;
        final double pureP99;
        final double pureP999;
        final double fullP50;
        final double fullP90;
        final double fullP99;
        final double fullP999;

        BenchmarkResult(
                int inflight,
                double rps,
                double pureP50,
                double pureP90,
                double pureP99,
                double pureP999,
                double fullP50,
                double fullP90,
                double fullP99,
                double fullP999
        ) {
            this.inflight = inflight;
            this.rps = rps;
            this.pureP50 = pureP50;
            this.pureP90 = pureP90;
            this.pureP99 = pureP99;
            this.pureP999 = pureP999;
            this.fullP50 = fullP50;
            this.fullP90 = fullP90;
            this.fullP99 = fullP99;
            this.fullP999 = fullP999;
        }

        @Override
        public String toString() {
            return String.format(
                    "Inflight: %d, RPS: %.2f, Pure P50: %.2fµs, Pure P90: %.2fµs, Pure P99: %.2fµs, Pure P99.9: %.2fµs, Full P50: %.2fµs, Full P90: %.2fµs, Full P99: %.2fµs, Full P99.9: %.2fµs",
                    inflight, rps, pureP50, pureP90, pureP99, pureP999, fullP50, fullP90, fullP99, fullP999
            );
        }
    }

    private static void printCSV(List<BenchmarkResult> results) {
        System.out.print("What");
        for (BenchmarkResult result : results) {
            System.out.printf(",%d", result.inflight);
        }
        System.out.println();

        System.out.print("Pure latency p50 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.pureP50);
        }
        System.out.println();

        System.out.print("Pure latency p90 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.pureP90);
        }
        System.out.println();

        System.out.print("Pure latency p99 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.pureP99);
        }
        System.out.println();

        System.out.print("Pure latency p99.9 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.pureP999);
        }
        System.out.println();

        System.out.print("Full latency p50 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.fullP50);
        }
        System.out.println();

        System.out.print("Full latency p90 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.fullP90);
        }
        System.out.println();

        System.out.print("Full latency p99 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.fullP99);
        }
        System.out.println();

        System.out.print("Full latency p99.9 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.fullP999);
        }
        System.out.println();

        System.out.print("Throughput (RPS)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.rps);
        }
        System.out.println();
    }

    private static void printHuman(List<BenchmarkResult> results) {
        // Calculate column widths
        int inflightWidth = Math.max(8, String.valueOf(results.get(results.size() - 1).inflight).length());
        int rpsWidth = Math.max(12, results.stream()
                .mapToInt(r -> String.format("%.2f", r.rps).length())
                .max()
                .orElse(12));
        int latencyWidth = Math.max("Full P99.9 (µs)".length(), results.stream()
                .flatMapToInt(r -> Stream.of(
                                r.pureP50,
                                r.pureP90,
                                r.pureP99,
                                r.pureP999,
                                r.fullP50,
                                r.fullP90,
                                r.fullP99,
                                r.fullP999
                        )
                        .mapToInt(v -> String.format("%.2f", v).length()))
                .max()
                .orElse(10));

        // Print header
        String headerFormat =
                "%-" + inflightWidth + "s  %-" + rpsWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s  %-" +
                        latencyWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s  %-" +
                        latencyWidth + "s  %-" + latencyWidth + "s%n";
        int separatorWidth = inflightWidth + rpsWidth + latencyWidth * 8 + 18;
        System.out.println("\nBenchmark Results");
        System.out.println("=".repeat(separatorWidth));
        System.out.printf(
                headerFormat,
                "Inflight",
                "RPS",
                "Pure P50 (µs)",
                "Pure P90 (µs)",
                "Pure P99 (µs)",
                "Pure P99.9 (µs)",
                "Full P50 (µs)",
                "Full P90 (µs)",
                "Full P99 (µs)",
                "Full P99.9 (µs)"
        );
        System.out.println("-".repeat(separatorWidth));

        // Print results
        String rowFormat =
                "%-" + inflightWidth + "d  %" + rpsWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f  %" +
                        latencyWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f  %" +
                        latencyWidth + ".2f  %" + latencyWidth + ".2f%n";
        for (BenchmarkResult result : results) {
            System.out.printf(rowFormat,
                    result.inflight,
                    result.rps,
                    result.pureP50,
                    result.pureP90,
                    result.pureP99,
                    result.pureP999,
                    result.fullP50,
                    result.fullP90,
                    result.fullP99,
                    result.fullP999);
        }
        System.out.println("=".repeat(separatorWidth));
    }

    private static void printResults(List<BenchmarkResult> results, OutputFormat format) {
        if (format == OutputFormat.CSV) {
            printCSV(results);
        } else {
            printHuman(results);
        }
    }

    private static class RunnerResult {
        final List<Long> pureLatencies;
        final List<Long> fullLatencies;

        RunnerResult(List<Long> pureLatencies, List<Long> fullLatencies) {
            this.pureLatencies = pureLatencies;
            this.fullLatencies = fullLatencies;
        }
    }

    private static class QueryRunner implements Callable<RunnerResult> {
        private final String url;
        private final long durationNanos;
        private final AtomicInteger completed;
        private final AtomicInteger errors;
        private final Limiter<Void> limiter;

        QueryRunner(String url, long durationNanos, AtomicInteger completed, AtomicInteger errors, Limiter<Void> limiter) {
            this.url = url;
            this.durationNanos = durationNanos;
            this.completed = completed;
            this.errors = errors;
            this.limiter = limiter;
        }

        @Override
        public RunnerResult call() throws Exception {
            List<Long> pureLatencies = new ArrayList<>();
            List<Long> fullLatencies = new ArrayList<>();
            Connection conn = null;
            PreparedStatement stmt = null;
            try {
                conn = DriverManager.getConnection(url);
                stmt = conn.prepareStatement("SELECT 1");
                long startTime = System.nanoTime();
                while (System.nanoTime() - startTime < durationNanos) {
                    long fullStart = System.nanoTime();
                    Limiter.Listener listener = null;
                    boolean listenerReleased = false;
                    try {
                        if (limiter != null) {
                            Optional<Limiter.Listener> maybeListener = limiter.acquire(null);
                            if (!maybeListener.isPresent()) {
                                errors.incrementAndGet();
                                continue;
                            }
                            listener = maybeListener.get();
                        }

                        long pureStart = System.nanoTime();
                        try (ResultSet rs = stmt.executeQuery()) {
                            if (rs.next()) {
                                long end = System.nanoTime();
                                pureLatencies.add(end - pureStart);
                                fullLatencies.add(end - fullStart);
                                completed.incrementAndGet();
                                if (listener != null) {
                                    listener.onSuccess();
                                    listenerReleased = true;
                                }
                            } else if (listener != null) {
                                listener.onIgnore();
                                listenerReleased = true;
                            }
                        }
                    } catch (Exception e) {
                        System.err.printf("Error executing query: %s%n", e.getMessage());
                        errors.incrementAndGet();
                        if (listener != null && !listenerReleased) {
                            listener.onDropped();
                            listenerReleased = true;
                        }
                        // Sleep a bit to avoid tight error loop
                        Thread.sleep(100);
                    } finally {
                        if (listener != null && !listenerReleased) {
                            listener.onIgnore();
                        }
                    }
                }
            } catch (Exception e) {
                System.err.printf("Error in query runner: %s%n", e.getMessage());
                errors.incrementAndGet();
                throw e;
            } finally {
                if (stmt != null) {
                    try {
                        stmt.close();
                    } catch (Exception e) {
                        System.err.printf("Error closing statement: %s%n", e.getMessage());
                    }
                }
                if (conn != null) {
                    try {
                        conn.close();
                    } catch (Exception e) {
                        System.err.printf("Error closing connection: %s%n", e.getMessage());
                    }
                }
            }
            return new RunnerResult(pureLatencies, fullLatencies);
        }
    }

    private static Limit createLimitAlgorithm(LimiterAlgorithm limiterAlgorithm, int limiterInitialLimit) {
        switch (limiterAlgorithm) {
            case VEGAS:
                return VegasLimit.newBuilder()
                        .initialLimit(limiterInitialLimit)
                        .build();
            case GRADIENT:
                return GradientLimit.newBuilder()
                        .initialLimit(limiterInitialLimit)
                        .build();
            case AIMD:
                return AIMDLimit.newBuilder()
                        .initialLimit(limiterInitialLimit)
                        .minLimit(1)
                        .build();
            default:
                throw new IllegalArgumentException("Unsupported limiter algorithm: " + limiterAlgorithm);
        }
    }

    private static Limiter<Void> createQueryLimiter(
            boolean useConcurrencyLimits,
            int limiterInitialLimit,
            String limiterName,
            LimiterAlgorithm limiterAlgorithm
    ) {
        if (!useConcurrencyLimits) {
            return null;
        }
        Limiter<Void> baseLimiter = SimpleLimiter.newBuilder()
                .named(limiterName)
                .limit(createLimitAlgorithm(limiterAlgorithm, limiterInitialLimit))
                .build();
        return BlockingLimiter.wrap(baseLimiter);
    }

    private static BenchmarkResult runBenchmark(
            String url,
            int inflight,
            int intervalSeconds,
            boolean useConcurrencyLimits,
            int limiterInitialLimit,
            String limiterName,
            LimiterAlgorithm limiterAlgorithm
    ) throws Exception {
        ExecutorService executor = Executors.newFixedThreadPool(inflight);
        List<Future<RunnerResult>> futures = new ArrayList<>(inflight);
        AtomicInteger completed = new AtomicInteger(0);
        AtomicInteger errors = new AtomicInteger(0);
        Limiter<Void> limiter = createQueryLimiter(useConcurrencyLimits, limiterInitialLimit, limiterName, limiterAlgorithm);
        long startTime = System.nanoTime();

        try {
            for (int i = 0; i < inflight; i++) {
                futures.add(executor.submit(new QueryRunner(url, intervalSeconds * 1_000_000_000L, completed, errors, limiter)));
            }

            List<Long> allPureLatencies = new ArrayList<>();
            List<Long> allFullLatencies = new ArrayList<>();
            for (Future<RunnerResult> future : futures) {
                try {
                    RunnerResult result = future.get();
                    allPureLatencies.addAll(result.pureLatencies);
                    allFullLatencies.addAll(result.fullLatencies);
                } catch (Exception e) {
                    System.err.printf("Error getting future result: %s%n", e.getMessage());
                }
            }

            long endTime = System.nanoTime();
            executor.shutdown();
            if (!executor.awaitTermination(1, TimeUnit.MINUTES)) {
                System.err.println("Timeout waiting for executor shutdown");
                executor.shutdownNow();
            }

            double durationSeconds = (endTime - startTime) / 1e9;
            double rps = completed.get() / durationSeconds;

            if (allPureLatencies.isEmpty() || allFullLatencies.isEmpty()) {
                throw new RuntimeException("No successful queries completed");
            }

            double[] pureLatenciesMicros = allPureLatencies.stream().mapToDouble(l -> l / 1e3).toArray();
            double[] fullLatenciesMicros = allFullLatencies.stream().mapToDouble(l -> l / 1e3).toArray();
            Percentile percentile = new Percentile();
            double pureP50 = percentile.evaluate(pureLatenciesMicros, 50);
            double pureP90 = percentile.evaluate(pureLatenciesMicros, 90);
            double pureP99 = percentile.evaluate(pureLatenciesMicros, 99);
            double pureP999 = percentile.evaluate(pureLatenciesMicros, 99.9);
            double fullP50 = percentile.evaluate(fullLatenciesMicros, 50);
            double fullP90 = percentile.evaluate(fullLatenciesMicros, 90);
            double fullP99 = percentile.evaluate(fullLatenciesMicros, 99);
            double fullP999 = percentile.evaluate(fullLatenciesMicros, 99.9);

            if (errors.get() > 0) {
                System.err.printf("Warning: %d errors occurred during benchmark%n", errors.get());
            }

            return new BenchmarkResult(
                    inflight,
                    rps,
                    pureP50,
                    pureP90,
                    pureP99,
                    pureP999,
                    fullP50,
                    fullP90,
                    fullP99,
                    fullP999
            );
        } catch (Exception e) {
            executor.shutdownNow();
            throw e;
        }
    }

    private static Options createOptions() {
        Options options = new Options();
        options.addOption(Option.builder("j")
                .longOpt("jdbc-url")
                .hasArg()
                .desc("JDBC connection URL (overrides host/port/user/password options)")
                .build());
        options.addOption(Option.builder("h")
                .longOpt("host")
                .hasArg()
                .desc("PostgreSQL server hostname (default: localhost)")
                .build());
        options.addOption(Option.builder("p")
                .longOpt("port")
                .hasArg()
                .desc("PostgreSQL server port (default: " + DEFAULT_PORT + ")")
                .build());
        options.addOption(Option.builder("u")
                .longOpt("user")
                .hasArg()
                .desc("PostgreSQL username (default: postgres)")
                .build());
        options.addOption(Option.builder("w")
                .longOpt("password")
                .hasArg()
                .desc("PostgreSQL password (default: postgres)")
                .build());
        options.addOption(Option.builder("m")
                .longOpt("min-inflight")
                .hasArg()
                .desc("Minimum number of concurrent connections (default: " + DEFAULT_MIN_INFLIGHT + ")")
                .build());
        options.addOption(Option.builder("M")
                .longOpt("max-inflight")
                .hasArg()
                .desc("Maximum number of concurrent connections (default: " + DEFAULT_MAX_INFLIGHT + ")")
                .build());
        options.addOption(Option.builder("i")
                .longOpt("interval")
                .hasArg()
                .desc("Duration in seconds to run each inflight level (default: " + DEFAULT_INTERVAL_SECONDS + ")")
                .build());
        options.addOption(Option.builder("f")
                .longOpt("format")
                .hasArg()
                .desc("Output format: human or csv (default: " + DEFAULT_FORMAT + ")")
                .build());
        options.addOption(Option.builder("l")
                .longOpt("linear")
                .desc("Use linear inflight growth (+1); default growth is exponential (*2)")
                .build());
        options.addOption(Option.builder("c")
                .longOpt("use-concurrency-limits")
                .desc("Enable client-side concurrency limiter per query (blocking mode)")
                .build());
        options.addOption(Option.builder()
                .longOpt("limiter-initial-limit")
                .hasArg()
                .desc("Initial limiter value when concurrency-limits is enabled (default: " + DEFAULT_LIMITER_INITIAL_LIMIT + ")")
                .build());
        options.addOption(Option.builder()
                .longOpt("limiter-name")
                .hasArg()
                .desc("Limiter name when concurrency-limits is enabled (default: " + DEFAULT_LIMITER_NAME + ")")
                .build());
        options.addOption(Option.builder()
                .longOpt("limiter-algorithm")
                .hasArg()
                .desc("Limiter algorithm when concurrency-limits is enabled: vegas, gradient, or aimd (default: " + DEFAULT_LIMITER_ALGORITHM + ")")
                .build());
        return options;
    }

    public static void main(String[] args) {
        Options options = createOptions();
        CommandLineParser parser = new DefaultParser();
        HelpFormatter formatter = new HelpFormatter();

        try {
            CommandLine cmd = parser.parse(options, args);

            String jdbcUrl = cmd.getOptionValue("jdbc-url");
            if (jdbcUrl == null) {
                String hostname = cmd.getOptionValue("host", "localhost");
                String port = cmd.getOptionValue("port", DEFAULT_PORT);
                String username = cmd.getOptionValue("user", "postgres");
                String password = cmd.getOptionValue("password", "postgres");
                jdbcUrl = String.format("jdbc:postgresql://%s:%s/postgres?user=%s&password=%s",
                        hostname, port, username, password);
            }

            if (jdbcUrl.startsWith("jdbc:ydb:")) {
                Class.forName("tech.ydb.jdbc.YdbDriver");
            } else if (jdbcUrl.startsWith("jdbc:postgresql:")) {
                Class.forName("org.postgresql.Driver");
            }

            int minInflight = Integer.parseInt(cmd.getOptionValue("min-inflight", String.valueOf(DEFAULT_MIN_INFLIGHT)));
            int maxInflight = Integer.parseInt(cmd.getOptionValue("max-inflight", String.valueOf(DEFAULT_MAX_INFLIGHT)));
            int intervalSeconds = Integer.parseInt(cmd.getOptionValue("interval", String.valueOf(DEFAULT_INTERVAL_SECONDS)));
            boolean useConcurrencyLimits = cmd.hasOption("use-concurrency-limits");
            int limiterInitialLimit = Integer.parseInt(cmd.getOptionValue("limiter-initial-limit", String.valueOf(DEFAULT_LIMITER_INITIAL_LIMIT)));
            String limiterName = cmd.getOptionValue("limiter-name", DEFAULT_LIMITER_NAME);
            String limiterAlgorithmStr = cmd.getOptionValue("limiter-algorithm", DEFAULT_LIMITER_ALGORITHM).toLowerCase();
            LimiterAlgorithm limiterAlgorithm;
            try {
                limiterAlgorithm = LimiterAlgorithm.valueOf(limiterAlgorithmStr.toUpperCase());
            } catch (IllegalArgumentException e) {
                System.err.println("Invalid limiter-algorithm: " + limiterAlgorithmStr + ". Must be 'vegas', 'gradient', or 'aimd'");
                System.exit(1);
                return;
            }
            if (minInflight < 1 || maxInflight < 1) {
                System.err.println("min-inflight and max-inflight must be >= 1");
                System.exit(1);
                return;
            }
            if (minInflight > maxInflight) {
                System.err.println("min-inflight must be <= max-inflight");
                System.exit(1);
                return;
            }
            if (intervalSeconds < 1) {
                System.err.println("interval must be >= 1");
                System.exit(1);
                return;
            }
            if (limiterInitialLimit < 1) {
                System.err.println("limiter-initial-limit must be >= 1");
                System.exit(1);
                return;
            }
            if (limiterName.trim().isEmpty()) {
                System.err.println("limiter-name must not be empty");
                System.exit(1);
                return;
            }
            boolean linear = cmd.hasOption("linear");
            String formatStr = cmd.getOptionValue("format", DEFAULT_FORMAT).toLowerCase();
            OutputFormat format;
            try {
                format = OutputFormat.valueOf(formatStr.toUpperCase());
            } catch (IllegalArgumentException e) {
                System.err.println("Invalid format: " + formatStr + ". Must be 'human' or 'csv'");
                System.exit(1);
                return;
            }

            System.out.println("Running benchmark with parameters:");
            String limiterMode = useConcurrencyLimits
                    ? String.format("enabled(blocking, algorithm=%s, initial=%d, name=%s)", limiterAlgorithmStr, limiterInitialLimit, limiterName)
                    : "disabled";
            System.out.printf(
                    "JDBC URL: %s, Min Inflight: %d, Max Inflight: %d, Growth: %s, Interval: %d seconds, Format: %s, Limiter: %s%n",
                    jdbcUrl,
                    minInflight,
                    maxInflight,
                    linear ? "linear(+1)" : "exponential(*2)",
                    intervalSeconds,
                    formatStr,
                    limiterMode
            );

            List<BenchmarkResult> results = new ArrayList<>();
            for (int inflight = minInflight; inflight <= maxInflight; inflight = linear ? inflight + 1 : inflight * 2) {
                results.add(runBenchmark(
                        jdbcUrl,
                        inflight,
                        intervalSeconds,
                        useConcurrencyLimits,
                        limiterInitialLimit,
                        limiterName,
                        limiterAlgorithm
                ));
            }

            printResults(results, format);
        } catch (ParseException e) {
            System.err.println(e.getMessage());
            formatter.printHelp("select1-benchmark", options);
            System.exit(1);
        } catch (Exception e) {
            System.err.println("Error running benchmark: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }
}