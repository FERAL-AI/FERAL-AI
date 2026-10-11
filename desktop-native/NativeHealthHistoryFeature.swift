import Foundation
import SwiftUI
import Charts
import CoreFoundation

struct NativeHealthHistoryFailure:LocalizedError {
    let message:String
    init(_ message:String){self.message=message}
    var errorDescription:String?{message}
}
struct NativeHealthHistoryPoint:Identifiable {
    let id:UUID
    let sampledAt:Date
    let value:Double
    // Inspection is evidence of the stored reading, so canonical display
    // precision must not erase fractional information here.
    var recordedValue:String{String(value)}
}
struct NativeHealthHistorySeries:Identifiable {
    let metric:String,source:String,label:String,sourceName:String,unit:String
    let precision:Int
    let points:[NativeHealthHistoryPoint]
    var id:String{metric+"|"+source+"|"+unit}
}
struct NativeHealthHistorySnapshot {
    let windowDays:Int
    let series:[NativeHealthHistorySeries]
    var count:Int{series.reduce(0,{$0+$1.points.count})}
}
enum NativeHealthHistoryWire {
    static let windows=[7,30,90,180]
    static let maxBytes=2097152,maxSeries=64,maxSeriesPoints=4096,maxTotalPoints=16384
    static func number(_ value:Any?)->Double?{guard let n=value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else{return nil};return n.doubleValue}
    static func text(_ value:Any?,limit:Int,empty:Bool=false)->String?{guard let s=value as? String,(empty || !s.isEmpty),s.utf8.count<=limit,!s.unicodeScalars.contains(where:{$0.value<32 || $0.value==127}) else{return nil};return s}
    static func identifier(_ value:Any?,empty:Bool=false)->String?{guard let s=text(value,limit:128,empty:empty),s.isEmpty || s.range(of:"^[A-Za-z0-9_.:-]+$",options:.regularExpression) != nil else{return nil};return s}
    static func url(base:URL,days:Int)throws->URL {
        guard windows.contains(days),base.scheme=="http",["127.0.0.1","::1","[::1]"].contains(base.host ?? ""),base.user==nil,base.password==nil,var components=URLComponents(url:base,resolvingAgainstBaseURL:false) else{throw NativeHealthHistoryFailure("Recorded history requires the local service and a supported time window.")}
        components.path="/api/health/frame";components.queryItems=[URLQueryItem(name:"event_type",value:"vitals_trend"),URLQueryItem(name:"days",value:String(days)),URLQueryItem(name:"push",value:"0")];components.fragment=nil
        guard let url=components.url else{throw NativeHealthHistoryFailure("Invalid recorded-history URL.")};return url
    }
    static func parse(_ frame:[String:Any],days:Int)throws->NativeHealthHistorySnapshot {
        guard windows.contains(days),JSONSerialization.isValidJSONObject(frame),let bytes=try? JSONSerialization.data(withJSONObject:frame),bytes.count<=maxBytes,
              frame["type"] as? String=="health_update",let payload=frame["payload"] as? [String:Any],payload["event_type"] as? String=="vitals_trend",
              let data=payload["data"] as? [String:Any],number(data["window_days"])==Double(days),let rows=data["series"] as? [[String:Any]],rows.count<=maxSeries else{throw NativeHealthHistoryFailure("Recorded history returned an unsupported or oversized frame.")}
        // Older trend frames mislabeled mixed sources with the first source.
        // Require the corrected backend provenance contract for populated data.
        guard rows.isEmpty || data["source_grouping"] as? String=="metric_source_unit" else{throw NativeHealthHistoryFailure("This service cannot verify source-separated history. Update the local service before displaying recorded samples.")}
        var series:[NativeHealthHistorySeries]=[],ids:Set<String>=[],total=0
        for row in rows {
            guard let metric=identifier(row["metric"]),let source=identifier(row["source"],empty:true),let label=text(row["label"],limit:128),let unit=text(row["unit"],limit:32,empty:true),
                  let sourceName=text(row["source_name"],limit:128,empty:true),let precision=number(row["precision"]),precision.rounded()==precision,(0...6).contains(precision),
                  let entries=row["points"] as? [[String:Any]],!entries.isEmpty,entries.count<=maxSeriesPoints,ids.insert(metric+"|"+source+"|"+unit).inserted else{throw NativeHealthHistoryFailure("Recorded history has unsupported series identifiers or metadata.")}
            total+=entries.count;guard total<=maxTotalPoints else{throw NativeHealthHistoryFailure("Recorded history exceeds the native sample budget. Select a shorter window.")}
            var points:[NativeHealthHistoryPoint]=[]
            for entry in entries {
                guard let ts=number(entry["ts"]),ts>0,ts<=253402300799,let value=number(entry["value"]),
                      (entry["source"] as? String).map({$0==source}) ?? true else{throw NativeHealthHistoryFailure("Recorded history has an invalid timestamp, value or conflicting source.")}
                points.append(NativeHealthHistoryPoint(id:UUID(),sampledAt:Date(timeIntervalSince1970:ts),value:value))
            }
            // Duplicate timestamps are retained: simultaneous readings from a
            // source are not silently averaged or deduplicated by this view.
            points.sort{$0.sampledAt<$1.sampledAt}
            series.append(NativeHealthHistorySeries(metric:metric,source:source,label:label,sourceName:sourceName,unit:unit,precision:Int(precision),points:points))
        }
        return NativeHealthHistorySnapshot(windowDays:days,series:series.sorted{$0.id<$1.id})
    }
}
private final class NativeHealthHistoryRedirectGuard:NSObject,URLSessionTaskDelegate {
    func urlSession(_ session:URLSession,task:URLSessionTask,willPerformHTTPRedirection response:HTTPURLResponse,newRequest request:URLRequest,completionHandler:@escaping(URLRequest?)->Void){completionHandler(nil)}
    static func session()->URLSession{let config=URLSessionConfiguration.ephemeral;config.timeoutIntervalForRequest=20;config.timeoutIntervalForResource=30;return URLSession(configuration:config,delegate:NativeHealthHistoryRedirectGuard(),delegateQueue:nil)}
}
@MainActor final class NativeHealthHistoryModel:ObservableObject {
    @Published private(set) var selectedDays=7
    @Published private(set) var snapshot:NativeHealthHistorySnapshot?
    @Published private(set) var loading=false
    @Published private(set) var error:String?
    @Published private(set) var fetchedAt:Date?
    private var baseURL:URL?
    private var generation=UUID()
    private let session:URLSession
    init(session:URLSession?=nil){self.session=session ?? NativeHealthHistoryRedirectGuard.session()}
    func configure(baseURL:URL?){guard self.baseURL != baseURL else{return};generation=UUID();self.baseURL=baseURL;snapshot=nil;error=nil;fetchedAt=nil;loading=false}
    func selectWindow(_ days:Int){guard NativeHealthHistoryWire.windows.contains(days),selectedDays != days else{return};generation=UUID();selectedDays=days;loading=false;error=nil}
    func refresh()async {
        guard !loading else{return}
        guard let baseURL=baseURL else{error="The local service is not ready for recorded history.";return}
        let started=generation,days=selectedDays;loading=true
        defer{if generation==started{loading=false}}
        do {
            let url=try NativeHealthHistoryWire.url(base:baseURL,days:days)
            let (bytes,response)=try await session.data(from:url)
            guard bytes.count<=NativeHealthHistoryWire.maxBytes,let http=response as? HTTPURLResponse,(200..<300).contains(http.statusCode),
                  let frame=(try? JSONSerialization.jsonObject(with:bytes)) as? [String:Any],frame["error"]==nil else{throw NativeHealthHistoryFailure("Recorded history could not be read. Private service details are withheld.")}
            let result=try NativeHealthHistoryWire.parse(frame,days:days)
            guard generation==started,!Task.isCancelled else{return};snapshot=result;fetchedAt=Date();error=nil
        }catch{
            guard generation==started,!Task.isCancelled else{return}
            self.error=(error as? NativeHealthHistoryFailure)?.message ?? "Recorded history could not be refreshed. Private service details are withheld."
        }
    }
}
struct NativeHealthHistoryFeatureView:View {
    let baseURL:URL?
    @StateObject private var model=NativeHealthHistoryModel()
    var body:some View {
        VStack(alignment:.leading,spacing:14) {
            HStack{Text("Recorded history").font(.title2.bold());Spacer();if model.loading{ProgressView().controlSize(.small)};Button("Refresh recorded samples"){Task{await model.refresh()}}.disabled(model.loading || baseURL==nil)}
            Text("Reads stored samples from the local service. No connected-device query, cloud-provider fetch or phone broadcast is requested. Points show recorded timestamps; fetch time is separate. Gaps are left empty and samples are never interpolated.").font(.caption).foregroundStyle(.secondary)
            Picker("Recorded window",selection:Binding(get:{model.selectedDays},set:{model.selectWindow($0);Task{await model.refresh()}})){ForEach(NativeHealthHistoryWire.windows,id:\.self){days in Text("\(days) days").tag(days)}}.pickerStyle(.segmented)
            if let error=model.error{NativeSelectableText(error).foregroundStyle(.orange)}
            if let snapshot=model.snapshot {
                if snapshot.windowDays != model.selectedDays || model.error != nil || model.loading{Text("Showing previously fetched \(snapshot.windowDays)-day history. It does not represent a successful refresh of the selected window.").font(.caption).foregroundStyle(.orange)}
                Text("\(snapshot.count) stored samples across \(snapshot.series.count) metric/source series · \(snapshot.windowDays) days").font(.callout)
                if let fetched=model.fetchedAt{Text("Fetched \(fetched.formatted(date:.abbreviated,time:.standard)); this is not measurement time.").font(.caption).foregroundStyle(.secondary)}
                if snapshot.series.isEmpty{Text("No stored samples were returned for this window. No values, continuous coverage or sensor connection are inferred.").foregroundStyle(.secondary)}
                ForEach(snapshot.series){series in NativeHealthHistorySeriesView(series:series)}
            }else if !model.loading{Text("Recorded history is unavailable. No measurements are inferred.").foregroundStyle(.secondary)}
        }.padding(16).task(id:baseURL){model.configure(baseURL:baseURL);await model.refresh()}
    }
}
private struct NativeHealthHistorySeriesView:View {
    let series:NativeHealthHistorySeries
    @State private var expanded=false
    var body:some View {
        GroupBox {
            VStack(alignment:.leading,spacing:10) {
                Text(series.label+(series.unit.isEmpty ? "" : " ("+series.unit+")")).font(.headline)
                NativeSelectableText("Recorded source: "+(series.source.isEmpty ? "Unavailable" : (series.sourceName.isEmpty ? series.source : series.sourceName)+" ["+series.source+"]")).font(.caption)
                Text("\(series.points.count) stored samples · No continuous sensor coverage is inferred.").font(.caption).foregroundStyle(.secondary)
                if let first=series.points.first,let last=series.points.last{Text(first.sampledAt.formatted(date:.abbreviated,time:.standard)+" → "+last.sampledAt.formatted(date:.abbreviated,time:.standard)).font(.caption)}
                Chart(series.points){point in
                    PointMark(x:.value("Recorded time",point.sampledAt),y:.value(series.unit.isEmpty ? series.label : series.unit,point.value))
                        .symbolSize(14)
                        .accessibilityLabel(point.sampledAt.formatted(date:.abbreviated,time:.standard))
                        .accessibilityValue(point.recordedValue+(series.unit.isEmpty ? "" : " "+series.unit))
                }.frame(height:180).accessibilityLabel(series.label+", "+String(series.points.count)+" recorded points")
                DisclosureGroup("Inspect recorded points",isExpanded:$expanded){
                    ForEach(Array(series.points.prefix(200))){point in HStack{NativeSelectableText(point.sampledAt.formatted(date:.abbreviated,time:.standard)).font(.caption);Spacer();NativeSelectableText(point.recordedValue+(series.unit.isEmpty ? "" : " "+series.unit)).font(.caption)}}
                    if series.points.count>200{Text("First 200 points shown in this inspection table; the chart and count include all returned points.").font(.caption).foregroundStyle(.secondary)}
                }
            }.frame(maxWidth:.infinity,alignment:.leading)
        }
    }
}
