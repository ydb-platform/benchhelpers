#include <grpcpp/grpcpp.h>
#include <grpcpp/alarm.h>
#include <grpcpp/server_builder.h>
#include <grpcpp/server_context.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "debug.grpc.pb.h"
#include "debug.pb.h"

using grpc::Server;
using grpc::ServerAsyncResponseWriter;
using grpc::ServerBuilder;
using grpc::ServerCompletionQueue;
using grpc::ServerContext;
using grpc::Status;
using grpc::ServerAsyncReaderWriter;

using Ydb::Debug::V1::DebugService;
using Ydb::Debug::V1::PlainGrpcRequest;
using Ydb::Debug::V1::PlainGrpcResponse;

class ServerImpl final {
public:
    ServerImpl(const std::string& address, int num_cqs, int workers_per_cq, int callbacks_per_cq)
        : address_(address)
        , num_cqs_(num_cqs)
        , workers_per_cq_(workers_per_cq)
        , callbacks_per_cq_(callbacks_per_cq) {}

    ~ServerImpl() {
        server_->Shutdown();
        for (auto& cq : cqs_) {
            cq->Shutdown();
        }
    }

    void Run() {
        ServerBuilder builder;
        builder.AddListeningPort(address_, grpc::InsecureServerCredentials());
        builder.RegisterService(&service_);

        // Create completion queues
        for (int i = 0; i < num_cqs_; i++) {
            cqs_.emplace_back(builder.AddCompletionQueue());
        }

        server_ = builder.BuildAndStart();
        std::cout << "Server listening on " << address_ << std::endl;
        std::cout << "Configuration: " << num_cqs_ << " CQs, "
                  << workers_per_cq_ << " workers per CQ, "
                  << callbacks_per_cq_ << " callbacks per CQ" << std::endl;

        // Start worker threads for each CQ
        for (int i = 0; i < num_cqs_; i++) {
            for (int j = 0; j < workers_per_cq_; j++) {
                threads_.emplace_back([this, i]() { HandleRpcs(i); });
            }
        }

        // Create initial CallData and StreamCallData callback instances for each CQ
        for (int i = 0; i < num_cqs_; i++) {
            for (int j = 0; j < callbacks_per_cq_; j++) {
                new CallData(&service_, cqs_[i].get());
                new StreamCallData(&service_, cqs_[i].get());
            }
        }

        for (auto& thread : threads_) {
            thread.join();
        }
    }

private:
    class RequestCallback {
    public:
        RequestCallback(DebugService::AsyncService* service, ServerCompletionQueue* cq)
            : service_(service)
            , cq_(cq) {}

        virtual ~RequestCallback() = default;
        virtual void Proceed() = 0;

    protected:

        DebugService::AsyncService* service_;
        ServerCompletionQueue* cq_;
        ServerContext ctx_;
    };

    class CallData : public RequestCallback {
    public:
        CallData(DebugService::AsyncService* service, ServerCompletionQueue* cq)
            : RequestCallback(service, cq)
            , responder_(&ctx_)
        {
            Proceed();
        }

        void Proceed() override {
            switch (status_) {
                case CallStatus::CREATE:
                    status_ = CallStatus::PROCESS;
                    service_->RequestPingPlainGrpc(&ctx_, &request_, &responder_, cq_, cq_, this);
                    break;

                case CallStatus::PROCESS:
                    // Create a new CallData instance to handle the next request
                    new CallData(service_, cq_);

                    status_ = CallStatus::FINISH;
                    responder_.Finish(response_, Status::OK, this);
                    break;

                case CallStatus::FINISH:
                    delete this;
                    break;
            }
        }

    protected:
        enum class CallStatus { CREATE, PROCESS, FINISH };

        PlainGrpcRequest request_;
        PlainGrpcResponse response_;
        ServerAsyncResponseWriter<PlainGrpcResponse> responder_;
        CallStatus status_ = CallStatus::CREATE;
    };

    class StreamCallData : public RequestCallback {
    public:
        StreamCallData(DebugService::AsyncService* service, ServerCompletionQueue* cq)
            : RequestCallback(service, cq)
            , stream_(&ctx_)
            , stream_status_(StreamStatus::CREATE)
        {
            Proceed();
        }

        void Proceed() override {
            switch (stream_status_) {
                case StreamStatus::CREATE:
                    stream_status_ = StreamStatus::PROCESS;
                    service_->RequestPingStream(&ctx_, &stream_, cq_, cq_, this);
                    break;

                case StreamStatus::PROCESS:
                    // Create a new StreamCallData instance to handle the next request
                    new StreamCallData(service_, cq_);

                    stream_status_ = StreamStatus::READ;
                    [[fallthrough]];
                case StreamStatus::READ:
                    stream_status_ = StreamStatus::WRITE;
                    stream_.Read(&request_, this);
                    break;

                case StreamStatus::WRITE: {
                    stream_status_ = StreamStatus::READ;
                    response_.set_callbackts(std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::system_clock::now().time_since_epoch()).count());
                    stream_.Write(response_, this);
                    break;
                }

                case StreamStatus::FINISH:
                    delete this;
                    return;
            }
        }

        void SetStreamClosed() {
            if (stream_status_ == StreamStatus::READ || stream_status_ == StreamStatus::WRITE) {
                stream_status_ = StreamStatus::FINISH;
                stream_.Finish(Status::OK, this);
            }
        }

    protected:
        enum class StreamStatus { CREATE, PROCESS, READ, WRITE, FINISH };

        PlainGrpcRequest request_;
        PlainGrpcResponse response_;
        ServerAsyncReaderWriter<PlainGrpcResponse, PlainGrpcRequest> stream_;
        StreamStatus stream_status_;
    };

    void HandleRpcs(int cq_index) {
        void* tag;
        bool ok;

        while (true) {
            if (!cqs_[cq_index]->Next(&tag, &ok)) {
                break;
            }

            auto* callback = static_cast<RequestCallback*>(tag);
            if (!ok) {
                // Handle stream closure
                if (auto* stream_callback = dynamic_cast<StreamCallData*>(callback)) {
                    stream_callback->SetStreamClosed();
                    continue;
                }
            }
            callback->Proceed();
        }
    }

    std::string address_;
    int num_cqs_;
    int workers_per_cq_;
    int callbacks_per_cq_;
    DebugService::AsyncService service_;
    std::unique_ptr<Server> server_;
    std::vector<std::unique_ptr<ServerCompletionQueue>> cqs_;
    std::vector<std::thread> threads_;
};

void PrintUsage(const char* program_name) {
    std::cout << "Usage: " << program_name << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  -h, --help                Show this help message" << std::endl;
    std::cout << "  --host <hostname>         Server hostname (default: localhost)" << std::endl;
    std::cout << "  --port <port>             Server port (default: 2137)" << std::endl;
    std::cout << "  --num-cqs <N>             Number of completion queues (default: 1)" << std::endl;
    std::cout << "  --workers-per-cq <N>      Number of worker threads per completion queue (default: 1)" << std::endl;
    std::cout << "  --callbacks-per-cq <N>    Number of callbacks per completion queue (default: 100)" << std::endl;
}

int main(int argc, char** argv) {
    std::string host = "localhost";
    int port = 2137;
    int num_cqs = 1;
    int workers_per_cq = 1;
    int callbacks_per_cq = 100;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            PrintUsage(argv[0]);
            return 0;
        } else if (arg == "--host" && i + 1 < argc) {
            host = argv[++i];
        } else if (arg == "--port" && i + 1 < argc) {
            port = std::stoi(argv[++i]);
        } else if (arg == "--num-cqs" && i + 1 < argc) {
            num_cqs = std::stoi(argv[++i]);
        } else if (arg == "--workers-per-cq" && i + 1 < argc) {
            workers_per_cq = std::stoi(argv[++i]);
        } else if (arg == "--callbacks-per-cq" && i + 1 < argc) {
            callbacks_per_cq = std::stoi(argv[++i]);
        } else {
            std::cerr << "Unknown option: " << arg << std::endl;
            PrintUsage(argv[0]);
            return 1;
        }
    }

    std::string server_address = host + ":" + std::to_string(port);
    ServerImpl server(server_address, num_cqs, workers_per_cq, callbacks_per_cq);
    server.Run();

    return 0;
}
