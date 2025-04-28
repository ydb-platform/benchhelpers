#include <algorithm>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

#include <grpcpp/grpcpp.h>

// XXX
#include "build/_deps/grpc-src/src/core/lib/iomgr/socket_mutator.h"

#include <sys/socket.h>
#include <netinet/tcp.h>

#include "debug.pb.h"
#include "debug.grpc.pb.h"

using grpc::Channel;
using grpc::ClientContext;
using grpc::Status;
using Ydb::Debug::V1::DebugService;
using Ydb::Debug::V1::PlainGrpcRequest;
using Ydb::Debug::V1::PlainGrpcResponse;

class DebugServiceClient {
public:
    DebugServiceClient(std::shared_ptr<Channel> channel)
        : stub_(DebugService::NewStub(channel)), channel_(channel) {}

    uint64_t Ping() {
        PlainGrpcRequest request;
        PlainGrpcResponse response;
        ClientContext context;

        // Set a timeout of 1 second
        std::chrono::system_clock::time_point deadline =
            std::chrono::system_clock::now() + std::chrono::seconds(1);
        context.set_deadline(deadline);

        // Check channel state before making the request
        auto state = channel_->GetState(false);
        if (state != GRPC_CHANNEL_READY) {
            channel_->WaitForConnected(std::chrono::system_clock::now() + std::chrono::seconds(1));
            state = channel_->GetState(false);
            if (state != GRPC_CHANNEL_READY) {
                std::cerr << "Failed to connect to server. Channel state: " << state << std::endl;
                std::exit(1);
            }
        }

        auto start = std::chrono::high_resolution_clock::now();
        Status status = stub_->PingPlainGrpc(&context, request, &response);
        auto end = std::chrono::high_resolution_clock::now();

        if (!status.ok()) {
            std::cerr << "RPC failed: "
                      << "code=" << status.error_code()
                      << " message=\"" << status.error_message() << "\""
                      << " details=\"" << status.error_details() << "\""
                      << std::endl;

            std::cerr << "Channel state: " << channel_->GetState(false) << std::endl;
            std::cerr << "Service name: Ydb.Debug.V1.DebugService" << std::endl;
            std::cerr << "Method name: PingPlainGrpc" << std::endl;
            std::cerr << "Full method path: /Ydb.Debug.V1.DebugService/PingPlainGrpc" << std::endl;

            if (status.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED) {
                std::cerr << "Error: Request timed out after 1 second" << std::endl;
            } else if (status.error_code() == grpc::StatusCode::UNIMPLEMENTED) {
                std::cerr << "Error: Method PingPlainGrpc is not implemented on the server" << std::endl;
                std::cerr << "Please check if the server has the DebugService with PingPlainGrpc method implemented" << std::endl;
            }
            std::exit(1);
        }

        return std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
    }

    void StartStream() {
        stream_ = stub_->PingStream(&stream_context_);
    }

    uint64_t PingStream() {
        PlainGrpcRequest request;
        PlainGrpcResponse response;

        auto start = std::chrono::high_resolution_clock::now();
        if (!stream_->Write(request)) {
            std::cerr << "Failed to write to stream" << std::endl;
            std::exit(1);
        }

        if (!stream_->Read(&response)) {
            std::cerr << "Failed to read from stream" << std::endl;
            std::exit(1);
        }

        auto end = std::chrono::high_resolution_clock::now();
        return std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
    }

    void StopStream() {
        stream_->WritesDone();
        Status status = stream_->Finish();
        if (!status.ok()) {
            std::cerr << "Stream RPC failed: "
                      << "code=" << status.error_code()
                      << " message=\"" << status.error_message() << "\""
                      << " details=\"" << status.error_details() << "\""
                      << std::endl;
            std::exit(1);
        }
    }

private:
    std::unique_ptr<DebugService::Stub> stub_;
    std::shared_ptr<Channel> channel_;
    std::unique_ptr<grpc::ClientReaderWriter<PlainGrpcRequest, PlainGrpcResponse>> stream_;
    ClientContext stream_context_;
};

struct alignas(64) PerThreadResult {
    std::vector<uint64_t> latencies;
};

struct BenchmarkResult {
    int inflight;
    double throughput;
    uint64_t p50;
    uint64_t p90;
    uint64_t p99;
    uint64_t p99_9;
    uint64_t p100;
};

struct BenchmarkSettings {
    std::string host = "localhost";
    int port = 2137;
    int inflight = 32;
    int max_channels = 1;
    int interval_seconds = 10;
    int warmup_seconds = 1;
    bool use_local_pool = false;
    bool use_streaming = false;
};

struct BenchmarkFlags {
    bool use_range = false;
    bool with_csv = false;
    bool user_specified_max_channels = false;
    bool per_worker_stats = false;
    int min_inflight = 1;
    int max_inflight = 32;
};

void Worker(std::shared_ptr<grpc::Channel> channel, std::stop_token stop_token, std::atomic<bool>& startMeasure, PerThreadResult& result, bool use_streaming) {
    DebugServiceClient client(channel);

    if (use_streaming) {
        client.StartStream();
    }

    // warmup
    while (!startMeasure.load(std::memory_order_relaxed) && !stop_token.stop_requested()) {
        if (use_streaming) {
            client.PingStream();
        } else {
            client.Ping();
        }
    }

    // measure
    while (!stop_token.stop_requested()) {
        uint64_t latency = use_streaming ? client.PingStream() : client.Ping();
        result.latencies.push_back(latency);
    }

    if (use_streaming) {
        client.StopStream();
    }
}

void PrintStats(const std::vector<uint64_t>& latencies, int total_requests, int interval_seconds) {
    if (latencies.empty()) {
        std::cout << "No successful requests" << std::endl;
        return;
    }

    auto percentile = [&](double p) -> uint64_t {
        size_t index = static_cast<size_t>(p * latencies.size());
        return latencies[index];
    };

    double throughput = static_cast<double>(total_requests) / interval_seconds;

    std::cout << std::fixed << std::setprecision(2);
    std::cout << "Throughput: " << throughput << " req/s" << std::endl;
    std::cout << "Latency percentiles (us):" << std::endl;
    std::cout << "  50th: " << percentile(0.50) << std::endl;
    std::cout << "  90th: " << percentile(0.90) << std::endl;
    std::cout << "  99th: " << percentile(0.99) << std::endl;
    std::cout << "  99.9th: " << percentile(0.999) << std::endl;
    std::cout << "  100th: " << latencies.back() << std::endl;
}

BenchmarkResult CalculateStats(const std::vector<uint64_t>& latencies, int total_requests, int interval_seconds) {
    BenchmarkResult result;
    if (latencies.empty()) {
        return result;
    }

    auto percentile = [&](double p) -> uint64_t {
        size_t index = static_cast<size_t>(p * latencies.size());
        return latencies[index];
    };

    result.throughput = static_cast<double>(total_requests) / interval_seconds;
    result.p50 = percentile(0.50);
    result.p90 = percentile(0.90);
    result.p99 = percentile(0.99);
    result.p99_9 = percentile(0.999);
    result.p100 = latencies.back();

    return result;
}

void PrintUsage(const char* program_name) {
    BenchmarkSettings defaults;
    std::cout << "Usage: " << program_name << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  -h, --help           Show this help message" << std::endl;
    std::cout << "  --host <hostname>    Server hostname (default: " << defaults.host << ")" << std::endl;
    std::cout << "  --port <port>        Server port (default: " << defaults.port << ")" << std::endl;
    std::cout << "  --inflight <N>       Number of concurrent requests (default: " << defaults.inflight << ")" << std::endl;
    std::cout << "  --min-inflight <N>   Minimum number of concurrent requests for range test" << std::endl;
    std::cout << "  --max-inflight <N>   Maximum number of concurrent requests for range test" << std::endl;
    std::cout << "  --max-channels <N>   Maximum number of gRPC channels (default: " << defaults.max_channels << ")" << std::endl;
    std::cout << "  --interval <seconds> Benchmark duration in seconds (default: " << defaults.interval_seconds << ")" << std::endl;
    std::cout << "  --warmup <seconds>   Warmup duration in seconds (default: " << defaults.warmup_seconds << ")" << std::endl;
    std::cout << "  --with-csv           Output results in CSV format" << std::endl;
    std::cout << "  --streaming          Use bidirectional streaming RPC" << std::endl;
    std::cout << "  --local-pool         Use local subchannel pool for connection reuse" << std::endl;
    std::cout << "  --per-worker-stats   Show per-worker throughput statistics" << std::endl;
}

struct BenchmarkRunResult {
    BenchmarkResult stats;
    std::vector<PerThreadResult> thread_results;
};

BenchmarkRunResult RunBenchmark(const BenchmarkSettings& settings, int inflight) {
    std::vector<std::jthread> threads;
    std::vector<PerThreadResult> thread_results(inflight);
    std::stop_source stop_source;
    std::atomic<bool> startMeasure{false};

    // don't create extra channels: worker can use at most one
    int max_channels = std::min(inflight, settings.max_channels);

    // Create channels
    std::vector<std::shared_ptr<grpc::Channel>> channels;
    std::string target = settings.host + ":" + std::to_string(settings.port);
    grpc::ChannelArguments args;

    if (settings.use_local_pool) {
        args.SetInt(GRPC_ARG_USE_LOCAL_SUBCHANNEL_POOL, 1);
    }

    for (int i = 0; i < max_channels; ++i) {
        channels.push_back(grpc::CreateCustomChannel(target, grpc::InsecureChannelCredentials(), args));
    }

    std::cout << "\nRunning benchmark with " << inflight << " concurrent requests using " << max_channels << " channels..." << std::endl;

    // Start worker threads
    for (int i = 0; i < inflight; ++i) {
        auto channel = channels[i % max_channels];
        threads.emplace_back(Worker,
            channel,
            stop_source.get_token(),
            std::ref(startMeasure),
            std::ref(thread_results[i]),
            settings.use_streaming
        );
    }

    // Wait for all threads to start and warmup
    std::cout << "Warmup phase started..." << std::endl;
    std::this_thread::sleep_for(std::chrono::seconds(settings.warmup_seconds));
    std::cout << "Warmup phase completed, measuring..." << std::endl;

    startMeasure.store(true, std::memory_order_relaxed);

    auto start = std::chrono::high_resolution_clock::now();
    std::this_thread::sleep_for(std::chrono::seconds(settings.interval_seconds));

    stop_source.request_stop();
    for (auto& thread : threads) {
        thread.join();
    }

    auto end = std::chrono::high_resolution_clock::now();
    auto total_time = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();

    std::vector<uint64_t> all_latencies;
    for (const auto& result: thread_results) {
        all_latencies.insert(all_latencies.end(), result.latencies.begin(), result.latencies.end());
    }
    std::sort(all_latencies.begin(), all_latencies.end());

    std::cout << "Total requests: " << all_latencies.size() << std::endl;
    std::cout << "Total time: " << total_time << " us" << std::endl;
    PrintStats(all_latencies, all_latencies.size(), settings.interval_seconds);

    BenchmarkResult result = CalculateStats(all_latencies, all_latencies.size(), settings.interval_seconds);
    result.inflight = inflight;

    return BenchmarkRunResult{result, thread_results};
}

void PrintResultsTable(const std::vector<BenchmarkResult>& results) {
    if (results.empty()) {
        return;
    }

    // Calculate column widths
    size_t inflight_width = std::max(strlen("Inflight"),
        std::to_string(results.back().inflight).length());
    size_t throughput_width = std::max(strlen("Throughput (req/s)"),
        std::to_string(static_cast<int>(results.back().throughput)).length() + 3); // +3 for decimal places
    size_t p50_width = std::max(strlen("P50 (us)"),
        std::to_string(results.back().p50).length());
    size_t p90_width = std::max(strlen("P90 (us)"),
        std::to_string(results.back().p90).length());
    size_t p99_width = std::max(strlen("P99 (us)"),
        std::to_string(results.back().p99).length());
    size_t p99_9_width = std::max(strlen("P99.9 (us)"),
        std::to_string(results.back().p99_9).length());
    size_t p100_width = std::max(strlen("P100 (us)"),
        std::to_string(results.back().p100).length());

    // Print header
    std::cout << "\nBenchmark Results Summary:" << std::endl;
    std::cout << std::setw(inflight_width) << "Inflight" << " | "
              << std::setw(throughput_width) << "Throughput (req/s)" << " | "
              << std::setw(p50_width) << "P50 (us)" << " | "
              << std::setw(p90_width) << "P90 (us)" << " | "
              << std::setw(p99_width) << "P99 (us)" << " | "
              << std::setw(p99_9_width) << "P99.9 (us)" << " | "
              << std::setw(p100_width) << "P100 (us)" << std::endl;

    // Print separator line
    std::cout << std::string(inflight_width, '-') << "-+-"
              << std::string(throughput_width, '-') << "-+-"
              << std::string(p50_width, '-') << "-+-"
              << std::string(p90_width, '-') << "-+-"
              << std::string(p99_width, '-') << "-+-"
              << std::string(p99_9_width, '-') << "-+-"
              << std::string(p100_width, '-') << std::endl;

    // Print data rows
    for (const auto& result : results) {
        std::cout << std::setw(inflight_width) << result.inflight << " | "
                  << std::setw(throughput_width) << std::fixed << std::setprecision(2) << result.throughput << " | "
                  << std::setw(p50_width) << result.p50 << " | "
                  << std::setw(p90_width) << result.p90 << " | "
                  << std::setw(p99_width) << result.p99 << " | "
                  << std::setw(p99_9_width) << result.p99_9 << " | "
                  << std::setw(p100_width) << result.p100 << std::endl;
    }
}

void PrintResultsCSV(const std::vector<BenchmarkResult>& results) {
    std::cout << "\nCSV Results:" << std::endl;
    std::cout << "inflight,throughput,p50,p90,p99,p99_9,p100" << std::endl;
    for (const auto& result : results) {
        std::cout << std::fixed << std::setprecision(2)
                  << result.inflight << ","
                  << result.throughput << ","
                  << result.p50 << ","
                  << result.p90 << ","
                  << result.p99 << ","
                  << result.p99_9 << ","
                  << result.p100 << std::endl;
    }
}

void PrintWorkerThroughputTable(const std::vector<PerThreadResult>& thread_results, int interval_seconds) {
    if (thread_results.empty()) {
        return;
    }

    // Calculate column widths
    size_t worker_width = std::max(strlen("Worker ID"),
        std::to_string(thread_results.size() - 1).length());
    size_t throughput_width = std::max(strlen("Throughput (req/s)"), size_t(12)); // reasonable minimum width for float

    // Print header
    std::cout << "\nPer-Worker Throughput Statistics:" << std::endl;
    std::cout << std::setw(worker_width) << "Worker ID" << " | "
              << std::setw(throughput_width) << "Throughput (req/s)" << std::endl;

    // Print separator line
    std::cout << std::string(worker_width, '-') << "-+-"
              << std::string(throughput_width, '-') << std::endl;

    // Print data rows
    for (size_t i = 0; i < thread_results.size(); ++i) {
        double throughput = static_cast<double>(thread_results[i].latencies.size()) / interval_seconds;
        std::cout << std::setw(worker_width) << i << " | "
                  << std::setw(throughput_width) << std::fixed << std::setprecision(2) << throughput << std::endl;
    }
}

class TGRpcInterceptSocketMutator : public grpc_socket_mutator {
public:
    TGRpcInterceptSocketMutator()
    {
        grpc_socket_mutator_init(this, &VTable);
    }

    void Test() {
        if (Intercepted_fd1 == -1 && Intercepted_fd2 == -1) {
            std::cout << "Failed to intercept descriptord and test for Nagle" << std::endl;
        }

        if (Intercepted_fd1 != -1) {
            int flag = 0;
            socklen_t len = sizeof(flag);
            if (getsockopt(Intercepted_fd1, IPPROTO_TCP, TCP_NODELAY, &flag, &len) == -1) {
                perror("getsockopt TCP_NODELAY for fd1");
                return;
            }
            if (flag) {
                std::cout << "TCP_NODELAY is ENABLED (Nagle OFF)\n";
            } else {
                std::cout << "TCP_NODELAY is DISABLED (Nagle ON)\n";
            }
        }

        if (Intercepted_fd2 != -1) {
            int flag = 0;
            socklen_t len = sizeof(flag);
            if (getsockopt(Intercepted_fd2, IPPROTO_TCP, TCP_NODELAY, &flag, &len) == -1) {
                perror("getsockopt TCP_NODELAY for fd2");
                return;
            }
            if (flag) {
                std::cout << "TCP_NODELAY is ENABLED (Nagle OFF)\n";
            } else {
                std::cout << "TCP_NODELAY is DISABLED (Nagle ON)\n";
            }
        }
    }

private:
    static TGRpcInterceptSocketMutator* Cast(grpc_socket_mutator* mutator) {
        return static_cast<TGRpcInterceptSocketMutator*>(mutator);
    }

    static bool Mutate(int fd, grpc_socket_mutator* mutator) {
        Intercepted_fd1 = fd;
        return true;
    }

    static int Compare(grpc_socket_mutator* a, grpc_socket_mutator* b) {
        return 0;
    }

    static void Destroy(grpc_socket_mutator* mutator) {
        delete Cast(mutator);
    }

    static bool Mutate2(const grpc_mutate_socket_info* info, grpc_socket_mutator* mutator) {
        Intercepted_fd2 = info->fd;
        return true;
    }

    static grpc_socket_mutator_vtable VTable;

    static int Intercepted_fd1;
    static int Intercepted_fd2;
};

int TGRpcInterceptSocketMutator::Intercepted_fd1 = -1;
int TGRpcInterceptSocketMutator::Intercepted_fd2 = -1;

grpc_socket_mutator_vtable TGRpcInterceptSocketMutator::VTable =
{
    &TGRpcInterceptSocketMutator::Mutate,
    &TGRpcInterceptSocketMutator::Compare,
    &TGRpcInterceptSocketMutator::Destroy,
    &TGRpcInterceptSocketMutator::Mutate2
};

void TestNagleAlgorithm(const std::string& target) {
    using namespace grpc;

    ChannelArguments args;

    auto* mutator = new TGRpcInterceptSocketMutator();
    args.SetSocketMutator(mutator);

    auto channel = CreateCustomChannel(target, InsecureChannelCredentials(), args);

    // Force connection
    auto state = channel->GetState(false);
    if (state != GRPC_CHANNEL_READY) {
        channel->WaitForConnected(std::chrono::system_clock::now() + std::chrono::seconds(1));
        state = channel->GetState(false);
        if (state != GRPC_CHANNEL_READY) {
            std::cerr << "Failed to connect to server. Channel state: " << state << std::endl;
            std::exit(1);
        }
    }

    mutator->Test();
}

int main(int argc, char** argv) {
    BenchmarkSettings settings;
    BenchmarkFlags flags;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            PrintUsage(argv[0]);
            return 0;
        } else if (arg == "--host" && i + 1 < argc) {
            settings.host = argv[++i];
        } else if (arg == "--port" && i + 1 < argc) {
            settings.port = std::stoi(argv[++i]);
        } else if (arg == "--inflight" && i + 1 < argc) {
            settings.inflight = std::stoi(argv[++i]);
        } else if (arg == "--min-inflight" && i + 1 < argc) {
            flags.min_inflight = std::stoi(argv[++i]);
            flags.use_range = true;
        } else if (arg == "--max-inflight" && i + 1 < argc) {
            flags.max_inflight = std::stoi(argv[++i]);
            flags.use_range = true;
        } else if (arg == "--max-channels" && i + 1 < argc) {
            settings.max_channels = std::stoi(argv[++i]);
            flags.user_specified_max_channels = true;
        } else if (arg == "--interval" && i + 1 < argc) {
            settings.interval_seconds = std::stoi(argv[++i]);
        } else if (arg == "--warmup" && i + 1 < argc) {
            settings.warmup_seconds = std::stoi(argv[++i]);
        } else if (arg == "--with-csv") {
            flags.with_csv = true;
        } else if (arg == "--streaming") {
            settings.use_streaming = true;
        } else if (arg == "--local-pool") {
            settings.use_local_pool = true;
        } else if (arg == "--per-worker-stats") {
            flags.per_worker_stats = true;
        } else {
            std::cerr << "Unknown option: " << arg << std::endl;
            PrintUsage(argv[0]);
            return 1;
        }
    }

    TestNagleAlgorithm(settings.host + ":" + std::to_string(settings.port));

    if (!flags.user_specified_max_channels) {
        settings.max_channels = flags.use_range ? flags.max_inflight : settings.inflight;
    }

    std::vector<BenchmarkRunResult> results;

    if (flags.use_range) {
        if (flags.min_inflight > flags.max_inflight) {
            std::cerr << "Error: min-inflight cannot be greater than max-inflight" << std::endl;
            return 1;
        }
        for (int current_inflight = flags.min_inflight; current_inflight <= flags.max_inflight; ++current_inflight) {
            results.push_back(RunBenchmark(settings, current_inflight));
        }
    } else {
        results.push_back(RunBenchmark(settings, settings.inflight));
    }

    // Convert results for table printing
    std::vector<BenchmarkResult> stats_results;
    for (const auto& result : results) {
        stats_results.push_back(result.stats);
    }

    if (!flags.per_worker_stats) {
        PrintResultsTable(stats_results);
        std::cout << std::endl;
    }

    if (flags.with_csv) {
        PrintResultsCSV(stats_results);
    }

    if (flags.per_worker_stats) {
        // Print per-worker stats for the last run only
        if (!results.empty()) {
            PrintWorkerThroughputTable(results.back().thread_results, settings.interval_seconds);
        }
    }

    return 0;
}
