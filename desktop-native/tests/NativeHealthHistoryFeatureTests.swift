import Foundation

private final class HistoryFixture:URLProtocol {
    static var requests:[URLRequest]=[]
    static var frame:[String:Any]=[:]
    static var status=200
    static var delayed=false
    override class func canInit(with request:URLRequest)->Bool{true}
    override class func canonicalRequest(for request:URLRequest)->URLRequest{request}
    override func startLoading(){
        Self.requests.append(request)
        let data=try! JSONSerialization.data(withJSONObject:Self.frame),response=HTTPURLResponse(url:request.url!,statusCode:Self.status,httpVersion:nil,headerFields:nil)!
        let send={self.client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed);self.client?.urlProtocol(self,didLoad:data);self.client?.urlProtocolDidFinishLoading(self)}
        if Self.delayed{Self.delayed=false;DispatchQueue.global().asyncAfter(deadline:.now()+0.1,execute:send)}else{send()}
    }
    override func stopLoading(){}
}
@main struct NativeHealthHistoryFeatureTests {
    static var count=0
    @MainActor static func check(_ value:Bool,_ message:String){guard value else{fatalError("FAIL: "+message)};count+=1}
    @MainActor static func reject(_ body:()throws->Void,_ message:String){do{try body();fatalError("Accepted: "+message)}catch{count+=1}}
    static func series(_ source:String="jw_health_glasses",points:[[String:Any]]?=nil)->[String:Any]{["metric":"hr","label":"Heart Rate","unit":"bpm","source":source,"source_name":source,"precision":0,"points":points ?? [["ts":1000.0,"value":72.5,"source":source],["ts":2000.0,"value":74.0,"source":source]]]}
    static func frame(days:Int=7,rows:[[String:Any]]?=nil,marker:Any="metric_source_unit")->[String:Any]{["type":"health_update","ts":999999.0,"payload":["event_type":"vitals_trend","ts":999999.0,"data":["window_days":days,"source_grouping":marker,"series":rows ?? [series()],"note":"private-health-canary"]]]}
    @MainActor static func main()async throws {
        let parsed=try NativeHealthHistoryWire.parse(frame(rows:[series(),series("veepoo_wristband"),series("")]),days:7)
        check(parsed.series.count==3 && parsed.count==6,"same metric preserves separate known and unknown source series")
        check(parsed.series.allSatisfy{$0.points.first?.sampledAt.timeIntervalSince1970==1000},"actual sample timestamps retained instead of frame aggregation timestamp")
        check(parsed.series.first?.points.first?.value==72.5,"recorded values preserve precision without averaging")
        let exact=try NativeHealthHistoryWire.parse(frame(rows:[series(points:[["ts":1000.0,"value":71.25],["ts":2000.0,"value":72.75],["ts":3000.0,"value":80.5]])]),days:7)
        check(exact.series[0].precision==0 && exact.series[0].points.map(\.recordedValue)==["71.25","72.75","80.5"],"inspection and chart accessibility preserve fractional recorded values despite canonical display precision zero")
        check(exact.series[0].points.allSatisfy{Double($0.recordedValue)==$0.value},"recorded value inspection round-trips to stored Double without display rounding")
        let gaps=try NativeHealthHistoryWire.parse(frame(rows:[series(points:[["ts":9000.0,"value":3.0],["ts":1000.0,"value":2.0],["ts":1000.0,"value":1.0]])]),days:7)
        check(gaps.count==3 && gaps.series[0].points.first?.sampledAt.timeIntervalSince1970==1000 && gaps.series[0].points.last?.sampledAt.timeIntervalSince1970==9000,"gaps and duplicate timestamps retained without synthetic points")
        check(try NativeHealthHistoryWire.parse(frame(rows:[]),days:7).count==0,"explicit empty history contains no invented zero values")
        reject({_ = try NativeHealthHistoryWire.parse(frame(marker:"metric_source"),days:7)},"old mixed-source formatter cannot assert source-separated provenance")
        reject({_ = try NativeHealthHistoryWire.parse(frame(rows:[series(),series()]),days:7)},"duplicate metric/source/unit series rejected")
        let invalidPoints:[[String:Any]]=[["ts":0.0,"value":2.0],["ts":Double.infinity,"value":2.0],["ts":1000.0,"value":Double.nan],["ts":1000.0,"value":true],["ts":1000.0,"value":Date()],["ts":1000.0,"value":2.0,"source":"wrong"]]
        for point in invalidPoints {
            reject({_ = try NativeHealthHistoryWire.parse(frame(rows:[series(points:[point])]),days:7)},"invalid sample timestamp/value/provenance rejected before serialization")
        }
        reject({_ = try NativeHealthHistoryWire.parse(frame(rows:[series("source\nforged")]),days:7)},"control-character source ID rejected")
        reject({_ = try NativeHealthHistoryWire.parse(frame(days:30),days:7)},"window receipt must match requested days")
        reject({_ = try NativeHealthHistoryWire.parse(frame(rows:Array(repeating:series(),count:65)),days:7)},"series budget bounded")
        reject({_ = try NativeHealthHistoryWire.parse(frame(rows:[series(points:Array(repeating:["ts":1000.0,"value":2.0],count:4097))]),days:7)},"per-series sample budget bounded")
        let url=try NativeHealthHistoryWire.url(base:URL(string:"http://127.0.0.1:9464/old?secret=discard")!,days:30),query=URLComponents(url:url,resolvingAgainstBaseURL:false)!.queryItems!
        check(url.path=="/api/health/frame" && query.first{$0.name=="push"}?.value=="0" && query.first{$0.name=="event_type"}?.value=="vitals_trend" && query.first{$0.name=="days"}?.value=="30","exact offline trend request suppresses node broadcast")
        reject({_ = try NativeHealthHistoryWire.url(base:URL(string:"https://remote.example")!,days:7)},"non-loopback blocked")
        reject({_ = try NativeHealthHistoryWire.url(base:URL(string:"http://127.0.0.1")!,days:999)},"unbounded day request rejected")
        let config=URLSessionConfiguration.ephemeral;config.protocolClasses=[HistoryFixture.self]
        let model=NativeHealthHistoryModel(session:URLSession(configuration:config)),base=URL(string:"http://127.0.0.1:9464")!
        HistoryFixture.frame=frame();model.configure(baseURL:base);await model.refresh()
        check(model.snapshot?.count==2 && model.fetchedAt != nil && model.error==nil && !model.loading,"actual frame fetch is committed")
        check(HistoryFixture.requests.allSatisfy{$0.httpMethod=="GET" && $0.url?.path=="/api/health/frame"},"no provider hardware or write endpoint requested")
        HistoryFixture.frame=["error":"private-service-canary"];await model.refresh()
        check(model.snapshot?.count==2 && model.error?.contains("private-service-canary")==false,"failed refresh keeps explicitly stale data without private server error")
        HistoryFixture.frame=frame(days:30);model.selectWindow(30);await model.refresh()
        check(model.snapshot?.windowDays==30 && model.error==nil,"window selection commits matching bounded history")
        HistoryFixture.frame=frame(days:30);HistoryFixture.delayed=true
        let pending=Task{await model.refresh()};for _ in 0..<1000{if model.loading{break};try await Task.sleep(nanoseconds:1_000_000)}
        model.configure(baseURL:nil);await pending.value
        check(model.snapshot==nil && model.fetchedAt==nil && !model.loading,"origin removal invalidates late history and clears private samples")
        print("Native health history: \(count) assertions passed.")
    }
}
