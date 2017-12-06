package org.wikimedia.search.mjolnir

import org.scalatest.FunSuite
import scala.io.Source

class DBNSuite extends FunSuite {
  test("create session items") {
    val ir = new InputReader(1, 20, true)
    val item = ir.makeSessionItem(
      "foo", "enwiki",
      Array("Example", ".example", "example.com"),
      Array(false, true, false))

    assert(item.isDefined)
  }

  test("create session item from line") {
    val ir = new InputReader(1, 20, true)
    val line = s"""hash_digest${"\t"}foo${"\t"}enwiki${"\t"}intentWeight${"\t"}["Example", ".example", "example.com"]${"\t"}layout${"\t"}[false, true, false]"""

    val items = ir.read(Iterator(line))
    assert(items.length == 1)
    val item = items.head
    assert(item.clicks.length == 3)
    assert(item.urlIds.length == 3)
  }

  test("compare results vs python clickmodels") {
    val file = Source.fromURL(getClass.getResource("/dbn.data"))
    val ir = new InputReader(1, 20, true)
    val sessions = ir.read(file.getLines())
    val config = new Config(ir.maxQueryId, 0.5D, 1)
    val model = new DbnModel(0.9D, config)
    val urlRelevances = model.train(sessions)
    val relevances = ir.toRelevances(urlRelevances)

    assert(relevances.length == 8)

    val queries = relevances.map { x => x.query }.toSet
    assert(queries.size == 2)
    assert(queries.contains("12345"))
    assert(queries.contains("23456"))

    val regions = relevances.map { x => x.region }.toSet
    assert(regions.size == 1)
    assert(regions.contains("foowiki"))

    // Values sourced from python clickmodels implementation
    val expected = Map(
      ("23456", "1111") -> 0.1D,
      ("23456", "2222") -> 0.2322820037105751D,
      ("23456", "3333") -> 0.16054421768707483D,
      ("23456", "4444") -> 0.3424860853432282D,
      ("12345", "1111") -> 0.38878221072530095D,
      ("12345", "2222") -> 0.33396748936638354D,
      ("12345", "3333") -> 0.23195153695743548D,
      ("12345", "4444") -> 0.23523307569244717D)

    for( rel <- relevances) {
      assert(math.abs(expected((rel.query, rel.url)) - rel.relevance) < 0.0001D)
    }
  }

  test("backwards forwards") {
    val rel = new PositionRel(
      Array.fill(20)(0.5D), Array.fill(20)(0.5D)
    )
    val gamma = 0.9D
    val clicks = Array.fill(20)(false)

    val foo = DbnModel.getForwardBackwardEstimates(rel, gamma, clicks)
    val alpha = foo._1
    val beta = foo._2
    val x = alpha(0)(0) * beta(0)(0) + alpha(0)(1) * beta(0)(1)

    val ok: Array[Boolean] = alpha.zip(beta).map { case (a: Array[Double], b: Array[Double]) =>
      math.abs((a(0) * b(0) + a(1) * b(1)) / x - 1) < 0.00001D
    }

    assert(ok.forall(x => x))
  }

  test("session estimate") {
    // Values sourced from python clickmodels implementation
    val rel = new PositionRel(Array.fill(20)(0.5D), Array.fill(20)(0.5D))
    val clicks = Array.fill(20)(false)

    clicks(0) = true
    var sessionEstimate = DbnModel.getSessionEstimate(rel, 0.9D, clicks)
    assert(math.abs(sessionEstimate.a.sum - 10.4370D) < 0.0001D)
    assert(math.abs(sessionEstimate.s.sum - 0.8461D) < 0.0001D)
    assert(math.abs(sessionEstimate.s.sum - sessionEstimate.s(0)) < 0.0001D)

    clicks(10) = true
    sessionEstimate = DbnModel.getSessionEstimate(rel, 0.9D, clicks)
    assert(math.abs(sessionEstimate.a.sum - 6.4347D) < 0.0001D)
    assert(math.abs(sessionEstimate.s.sum - 0.8457D) < 0.0001D)
    assert(math.abs(sessionEstimate.s.sum - sessionEstimate.s(0) - sessionEstimate.s(10)) < 0.0001D)
  }

  // Takes ~3.5s on my laptop versus 90 seconds in python
  test("basic benchmark") {
    val nQueries = 5000
    val nSessionsPerQuery = 20
    val nIterations = 40
    val nResultsPerQuery = 20

    val r = new scala.util.Random(0)
    val ir = new InputReader(10, 20, true)
    val sessions = (0 until nQueries).flatMap { query =>
      val urls: Array[String] = (0 until nResultsPerQuery).map { _ => r.nextInt.toString }.toArray
      (0 until nSessionsPerQuery).flatMap { session =>
        val clicks = Array.fill(nResultsPerQuery)(false)
        do {
          clicks(r.nextInt(nResultsPerQuery)) = true
        } while(r.nextDouble > 0.95D)
        ir.makeSessionItem(query.toString, "region", urls, clicks)
      }
    }.toArray.toSeq


    assert(sessions.length == nQueries * nSessionsPerQuery)

    val config = new Config(ir.maxQueryId, 0.5D, nIterations)
    val dbn = new DbnModel(0.9D, config)
    val start = System.nanoTime()
    dbn.train(sessions)
    val took = System.nanoTime() - start
    println(s"Took ${took/1000000}ms")

    // Create a datafile that python clickmodels can read in to have fair comparison
    //import java.io.File
    //import java.io.PrintWriter

    //val writer = new PrintWriter(new File("/tmp/dbn.clickmodels"))
    //for ( s <- sessions) {
    //  // poor mans json serialization
    //  val layout = Array.fill(s.urlIds.length)("false").mkString("[", ",", "]")
    //  val clicks = s.clicks.map(_.toString).mkString("[", ",", "]")
    //  val urls = s.urlIds.map(_.toString).mkString("[\"", "\",\"", "\"]")
    //  writer.write(s"0\t${s.queryId}\tregion\t0\t$urls\t$layout\t$clicks\n")
    //}
    //writer.close()
  }
}
